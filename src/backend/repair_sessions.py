"""Explicit, backup-first maintenance repair. No title/time-based fuzzy merging.

Run with writers stopped. Dry-run is the default; --apply commits each verified
identity group atomically. DATABASE_URL is read only from the environment.
"""
import argparse
import json
import os
from collections import defaultdict
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .session_titles import real_prompt, task_title


def identities(connection):
    rows = connection.execute("""
        select s.user_id,s.source,s.id,s.source_session_id,
               encode(h.payload_hash,'hex') header_hash
        from public.sessions s left join public.events h
          on h.user_id=s.user_id and h.session_id=s.id and h.source_sequence=0
          and h.source_item_index=20000 and h.type='usage' and s.source='codex'
    """).fetchall()
    result = defaultdict(set)
    for row in rows:
        key = None
        if row['source'] == 'claude-code':
            try:
                key = 'native:' + str(UUID(row['source_session_id']))
            except ValueError:
                pass
        elif row['header_hash']:
            key = 'header:' + row['header_hash']
        if key:
            result[(row['user_id'],row['source'],key)].add(row['id'])
    return result


def merge_group(c, owner, source, key, ids):
    """Called inside one transaction. Keep all nonduplicate events and summaries."""
    c.execute("select pg_advisory_xact_lock(hashtextextended(%s,742019))", (str(owner),))
    sessions = c.execute('select * from public.sessions where user_id=%s and id=any(%s) order by id for update',
                         (owner,ids)).fetchall()
    if len(sessions) != len(ids) or any(s['source'] != source for s in sessions):
        raise ValueError('repair ownership/source mismatch')
    # A matching source identity with different raw bytes at the same record
    # position is a fork or rewrite, not an automatic duplicate repair.
    conflict = c.execute("""select 1 from public.events where user_id=%s and session_id=any(%s)
        group by source_sequence,source_item_index having count(distinct payload_hash)>1 limit 1""",
        (owner,ids)).fetchone()
    if conflict:
        raise ValueError('divergent transcript positions; manual review required')
    canonical = c.execute("""select session_id,count(*) n from public.events
        where user_id=%s and session_id=any(%s) group by session_id order by n desc,session_id limit 1""",
        (owner,ids)).fetchone()['session_id']
    days = [r['local_day'] for r in c.execute("""select distinct local_day from public.events
        where user_id=%s and session_id=any(%s) order by local_day""", (owner,ids))]
    summaries = c.execute("""select * from public.summaries where user_id=%s and session_id=any(%s)
        order by generated_at,id""", (owner,ids)).fetchall()
    c.execute("""create temp table repair_event_map on commit drop as
        select id,first_value(id) over (partition by source_sequence,source_item_index,payload_hash
          order by revision desc,(session_id=%s) desc,id) target
        from public.events where user_id=%s and session_id=any(%s)""", (canonical,owner,ids))
    c.execute('create unique index on repair_event_map(id)')
    # Keep final results in preference to running/unknown copies. For every
    # optional field retain the richest known value rather than replacing it
    # with NULL from another installation.
    order = "(t.status not in ('unknown','running')) desc,t.revision desc,length(t.output_preview) desc nulls last,t.id"
    fields = ('tool_name','status','input_preview','output_preview','input_size','output_size',
              'input_hash','output_hash','exit_code','ended_at','duration_ms')
    selections = ','.join(f'(array_agg(t.{field} order by {order}) filter(where t.{field} is not null))[1] {field}'
                          for field in fields)
    c.execute(f"""create temp table repair_tools on commit drop as
        select t.source_call_id,(array_agg(m.target order by {order}))[1] event_id,
          max(t.revision) revision,min(t.started_at) started_at,min(t.local_day) local_day,{selections}
        from public.tool_calls t join repair_event_map m on m.id=t.event_id
        where t.user_id=%s and t.session_id=any(%s) group by t.source_call_id""", (owner,ids))
    c.execute('delete from public.tool_calls where user_id=%s and session_id=any(%s)', (owner,ids))
    removed = c.execute('delete from public.events e using repair_event_map m where e.id=m.id and m.id<>m.target').rowcount
    c.execute('update public.events set session_id=%s where user_id=%s and session_id=any(%s)', (canonical,owner,ids))
    columns = ','.join(('source_call_id','event_id','revision','started_at','local_day',*fields))
    c.execute(f'insert into public.tool_calls(user_id,session_id,{columns}) select %s,%s,{columns} from repair_tools',
              (owner,canonical))
    c.execute('update private.session_identity_keys set session_id=%s where user_id=%s and session_id=any(%s)',
              (canonical,owner,ids))
    c.execute("""insert into private.session_identity_keys(user_id,source,identity_key,session_id)
        values(%s,%s,%s,%s) on conflict(user_id,source,identity_key) do update set session_id=excluded.session_id""",
        (owner,source,key,canonical))
    for s in sessions:
        c.execute("""insert into private.session_identity_keys values(%s,%s,%s,%s)
            on conflict(user_id,source,identity_key) do update set session_id=excluded.session_id""",
            (owner,source,f"device:{s['device_id']}:{s['source_session_id']}",canonical))
    # Save every prior TLDR, but mark its coverage stale after a merged input.
    # The delete trigger intentionally invalidates derived text; restore the
    # captured revisions only after the destructive part of this repair.
    c.execute('delete from public.summaries where user_id=%s and session_id=any(%s)', (owner,ids))
    c.execute('delete from private.summary_jobs where user_id=%s and session_id=any(%s)', (owner,ids))
    c.execute('delete from public.sessions where user_id=%s and id=any(%s) and id<>%s', (owner,ids,canonical))
    base = max([s['summary_input_version'] for s in sessions] + [s['input_revision'] for s in summaries])
    c.execute("""update public.sessions set summary_input_version=%s,started_at=%s,last_event_at=%s,
        started_day=%s where user_id=%s and id=%s""",
        (base+len(summaries)+1,min(s['started_at'] for s in sessions),max(s['last_event_at'] for s in sessions),
         min(s['started_day'] for s in sessions),owner,canonical))
    for index, summary in enumerate(summaries,1):
        c.execute("""insert into public.summaries(user_id,session_id,input_revision,tldr,outcome,unresolved,model,prompt_version,generated_at)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (owner,canonical,base+index,summary['tldr'],summary['outcome'],summary['unresolved'],
             summary['model'],summary['prompt_version'],summary['generated_at']))
    c.execute("select private.bump_day_versions(%s,%s::date[],true,true,true,true,true)", (owner,days))
    c.execute("""insert into public.rollup_dirty(user_id,local_day) select %s,d from unnest(%s::date[]) d
        on conflict(user_id,local_day) do update set generation=public.rollup_dirty.generation+1,dirtied_at=now()""", (owner,days))
    return {'canonical':canonical,'removed_sessions':len(ids)-1,'removed_events':removed,'retained_summaries':len(summaries)}


def backfill_titles(c):
    changed = 0
    for s in c.execute('select id,user_id,title,display_title,project_name from public.sessions order by id').fetchall():
        if s['display_title']:
            continue
        name = task_title(s['title'])
        prompt_at = None
        if not name:
            # Server-side cursor keeps a long session bounded in memory.
            with c.cursor(name=f"title_{s['id']}") as prompts:
                prompts.execute("""select content_preview,created_at from public.events where user_id=%s
                    and session_id=%s and type='user' and content_preview is not null order by created_at,id""", (s['user_id'],s['id']))
                for p in prompts:
                    name = task_title(p['content_preview'])
                    if name:
                        prompt_at = p['created_at']
                        break
        # Clear injected source titles as well; source transcripts stay untouched.
        c.execute("""update public.sessions set display_title=%s,title_origin='prompt',title_prompt_at=%s,
            title=case when %s then title else null end where user_id=%s and id=%s""",
            (name or f"{s['project_name'] or 'Coding'} session",prompt_at,bool(real_prompt(s['title'])),s['user_id'],s['id']))
        c.execute("""select private.bump_day_versions(%s,array(select distinct local_day from public.events
            where user_id=%s and session_id=%s),true,false,false)""", (s['user_id'],s['user_id'],s['id']))
        changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row,autocommit=True) as c:
        groups = identities(c)
        print(json.dumps({'duplicate_groups':sum(len(ids)>1 for ids in groups.values())}))
        if not args.apply:
            return
        for (owner,source,key),ids in groups.items():
            with c.transaction():
                c.execute("set local lock_timeout='5s'; set local statement_timeout='120s'")
                if len(ids)>1:
                    print(json.dumps(merge_group(c,owner,source,key,sorted(ids))))
                else:
                    c.execute('insert into private.session_identity_keys values(%s,%s,%s,%s) on conflict do nothing',
                              (owner,source,key,next(iter(ids))))
        with c.transaction():
            print(json.dumps({'titles_updated':backfill_titles(c)}))


if __name__ == '__main__':
    main()
