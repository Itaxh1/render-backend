import asyncio
import hashlib
import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.bulk_ingest import write_records
from backend.models import DevicePrincipal, IngestBatch
from backend.postgres import _batch_hash
from backend.repair_sessions import identities, merge_group, backfill_titles
from backend.session_titles import task_title


def device(c, user=None):
    user = user or uuid4()
    c.execute('insert into auth.users values(%s) on conflict do nothing',(user,))
    did = c.execute("""insert into public.devices(user_id,name,platform,token_hash,token_expires_at)
        values(%s,'fixture','test',%s,now()+interval '1 day') returning id""", (user,uuid4().bytes*2)).fetchone()['id']
    return DevicePrincipal(user_id=user,device_id=did)


def records(file='a', native=None, revision=1):
    result=[]
    for seq,kind in enumerate(['usage','user','tool','tool_result']):
        e={'session_id':file*64,'type':kind,'created_at':'2026-09-16T12:00:00Z',
           'local_day':'2026-09-16','content_preview':'Fix dashboard caching' if kind=='user' else None}
        if native:
            e['native_session_id']=str(native)
        if kind in ('tool','tool_result'):
            e.update(source_call_id='call',tool_name='Bash',tool_status='succeeded' if kind=='tool_result' else 'running')
        if kind=='tool_result':
            e['tool_output_preview']='All tests passed'
        result.append(dict(source='codex',source_file_id=file*64,sequence=seq,
                           item_index=20000 if seq==0 else 0,revision=revision,stage='enriched',
                           payload_hash=hashlib.sha256(str(seq).encode()).hexdigest(),event=e))
    return result


def batch(rows):
    return IngestBatch(protocol_version=1,batch_id=uuid4(),device_sequence=1,extractor_version=1,records=rows)


def ingest(dsn,p,rows):
    async def run():
        async with await psycopg.AsyncConnection.connect(dsn,row_factory=dict_row) as c:
            return await write_records(c,p,batch(rows))
    return asyncio.run(run())


def test_legacy_and_native_reimports_are_idempotent_with_late_updates(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        a=device(c); b=device(c,a.user_id); other=device(c)
    assert ingest(migrated_database,a,records())==(4,0)
    native=uuid4()
    assert ingest(migrated_database,b,records('b',native))==(0,4)
    update=records('b',native,2)[-1:]
    update[0]['event']['tool_output_preview']='New verified result'
    assert ingest(migrated_database,b,update)==(1,0)
    # Same hash at another sequence must not be removed (repeated context).
    repeat=records('b',native)[1:2]; repeat[0]['sequence']=20
    assert ingest(migrated_database,b,repeat)==(1,0)
    assert ingest(migrated_database,other,records('c',native))==(4,0)
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        assert c.execute('select count(*) n from public.sessions where user_id=%s',(a.user_id,)).fetchone()['n']==1
        row=c.execute('select output_preview,duration_ms from public.tool_calls where user_id=%s',(a.user_id,)).fetchone()
        assert row=={'output_preview':'New verified result','duration_ms':None}
        c.execute('delete from public.sessions where user_id=%s',(a.user_id,))
        new=device(c,a.user_id)
    assert ingest(migrated_database,new,records('d',native))==(0,4)


def test_legacy_receipt_hash_does_not_change_for_absent_native_id():
    value=batch(records())
    legacy=value.model_dump(mode='json')
    for r in legacy['records']:
        r['event'].pop('native_session_id')
    assert _batch_hash(value)==hashlib.sha256(json.dumps(legacy,sort_keys=True,separators=(',',':')).encode()).digest()


def test_repair_preserves_unique_events_tools_summaries_and_old_alias_retries(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        a=device(c); b=device(c,a.user_id)
    ingest(migrated_database,a,records())
    # Simulate pre-fix import: remove canonical header mapping before device B.
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        c.execute("delete from private.session_identity_keys where user_id=%s and identity_key like 'header:%%'",(a.user_id,))
    rows=records('b'); extra=records('b')[1].copy(); extra['sequence']=30; rows.append(extra)
    ingest(migrated_database,b,rows)
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        groups=identities(c)
        (owner,source,key),ids=next((k,v) for k,v in groups.items() if k[0]==a.user_id)
        assert len(ids)==2
        for sid in ids:
            c.execute("""insert into public.summaries(user_id,session_id,input_revision,tldr,outcome,model)
                values(%s,%s,1,'Saved TLDR','unknown','fixture')""",(owner,sid))
        report=merge_group(c,owner,source,key,sorted(ids))
        assert report['removed_sessions']==1 and report['removed_events']==4
        assert c.execute('select count(*) n from public.events where user_id=%s',(owner,)).fetchone()['n']==5
        assert c.execute('select count(*) n from public.summaries where user_id=%s',(owner,)).fetchone()['n']==2
        assert c.execute('select count(*) n from public.tool_calls where user_id=%s',(owner,)).fetchone()['n']==1
        assert c.execute('select count(*) n from public.day_versions where user_id=%s and purge>0',(owner,)).fetchone()['n']==1
    assert ingest(migrated_database,a,records())==(0,4)


def test_titles_ignore_setup_and_are_not_outcomes():
    assert task_title('# AGENTS.md instructions <INSTRUCTIONS>') is None
    assert task_title('<environment_context>setup') is None
    assert task_title('continue') is None
    assert task_title('Can you please fix dashboard caching? It is slow.')=='Fix dashboard caching'
    assert task_title('Update AGENTS.md instructions')=='Update AGENTS.md instructions'
    assert task_title('Remote Control is active · Continue here\n\nCreate a coding session dashboard')=='Create a coding session dashboard'


def test_concurrent_reconnect_and_account_delete_are_safe(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        a=device(c); b=device(c,a.user_id)
    async def run():
        async def upload(p, rows):
            async with await psycopg.AsyncConnection.connect(migrated_database,row_factory=dict_row) as c:
                return await write_records(c,p,batch(rows))
        return await asyncio.gather(upload(a,records()),upload(b,records('b')))
    assert sorted(asyncio.run(run()))==[(0,4),(4,0)]
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        c.execute('delete from auth.users where id=%s',(a.user_id,))
        assert c.execute('select count(*) n from private.session_identity_keys where user_id=%s',(a.user_id,)).fetchone()['n']==0


def test_title_backfill_skips_injected_setup(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        p=device(c)
    rows=records(); rows[1]['event']['content_preview']='# AGENTS.md instructions <INSTRUCTIONS>'
    ingest(migrated_database,p,rows)
    next_prompt=records()[1]; next_prompt['sequence']=20
    ingest(migrated_database,p,[next_prompt])
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        c.execute('update public.sessions set display_title=null,title=%s where user_id=%s',
                  ('# AGENTS.md instructions <INSTRUCTIONS>',p.user_id))
        backfill_titles(c)
        row=c.execute('select title,display_title from public.sessions where user_id=%s',(p.user_id,)).fetchone()
        assert row=={'title':None,'display_title':'Fix dashboard caching'}


def test_identity_keys_and_resolver_are_not_browser_accessible(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        assert not c.execute("select has_table_privilege('authenticated','private.session_identity_keys','SELECT') ok").fetchone()['ok']
        assert not c.execute("select has_function_privilege('authenticated',oid,'EXECUTE') ok from pg_proc where proname='resolve_session'").fetchone()['ok']


def test_batch_titles_reject_unknown_ids_and_injected_setup():
    from backend.repair_titles import validate
    assert validate('{"titles":[{"id":1,"title":"Dashboard activity caching"}]}',[1])[0].title=='Dashboard activity caching'
    for content in ['{"titles":[{"id":2,"title":"Other session title"}]}',
                    '{"titles":[{"id":1,"title":"<environment_context>"}]}']:
        with pytest.raises(ValueError):
            validate(content,[1])


def test_repair_deletes_more_than_one_chunk_atomically(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        a=device(c); b=device(c,a.user_id)
    for principal,file in [(a,'a'),(b,'b')]:
        if principal==b:
            with psycopg.connect(migrated_database,row_factory=dict_row) as c:
                c.execute("delete from private.session_identity_keys where user_id=%s and identity_key like 'header:%%'",(a.user_id,))
        ingest(migrated_database,principal,records(file))
        for start,end in [(10,410),(410,612)]:
            values=[]
            for seq in range(start,end):
                value=records(file)[1]; value['sequence']=seq; values.append(value)
            ingest(migrated_database,principal,values)
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        (owner,source,key),ids=next((k,v) for k,v in identities(c).items() if k[0]==a.user_id)
        report=merge_group(c,owner,source,key,sorted(ids))
        assert report['removed_events']==606
        assert c.execute('select count(*) n from public.events where user_id=%s',(owner,)).fetchone()['n']==606
