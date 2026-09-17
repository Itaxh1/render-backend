"""Operator verification: saved snapshots, independent counts, timings, optional real Grok.

Use with with-render-db.mjs. Prints counts/timings, never secrets or prompt text.
Only writes the application's new snapshots and an explicitly requested project document.
"""
import argparse
import asyncio
import json
import os
from time import perf_counter

from backend.insights import Insights
from backend.project_docs import read_docs, request_docs, run_next


async def main(args):
    service = Insights(os.environ['DATABASE_URL'])
    service.api_key = os.environ.get('XAI_API_KEY')
    service.model = os.environ.get('XAI_MODEL', 'grok-4.3')
    await service.pool.open(wait=True)
    try:
        async with service.pool.connection() as c:
            owner = (await (await c.execute('select id from auth.users where email=%s', (args.email,))).fetchone())['id']
        await service.snapshot(owner, 'America/Phoenix')
        async with service.pool.connection() as c:
            row = await (await c.execute('select * from private.account_insights where user_id=%s', (owner,))).fetchone()
        started = perf_counter()
        await service.refresh(row)
        print(json.dumps({'snapshot_rebuild_ms': round((perf_counter()-started)*1000,2)}), flush=True)
        timings = {}
        for name, operation in [('profile', service.snapshot), ('projects', service.projects)]:
            timings[name] = []
            for _ in range(5):
                start = perf_counter()
                result = await operation(owner, 'America/Phoenix')
                timings[name].append(round((perf_counter()-start)*1000,2))
            print(json.dumps({'read':name,'ms':timings[name],'bytes':len(json.dumps(result).encode())}), flush=True)
        p = (await service.snapshot(owner, 'America/Phoenix'))['profile']
        async with service.pool.connection() as c:
            await c.execute("set local statement_timeout='60s'")
            counts = await (await c.execute("""select count(*) filter(where type='tool') tools,
                count(distinct local_day) filter(where type in ('user','agent','tool') and local_day<=current_date) days
                from public.events where user_id=%s""", (owner,))).fetchone()
            plan = await (await c.execute('explain (analyze,format json) select profile,projects from private.account_insights where user_id=%s', (owner,))).fetchone()
        assert p['toolCalls'] == counts['tools'], (p['toolCalls'],counts['tools'])
        assert p['activeDays'] == counts['days']
        print(json.dumps({'verified':True,'tools':p['toolCalls'],'active_days':p['activeDays'], 'prompts':p['prompts'],
                          'project_labels':p['projectsTotal'],'snapshot_sql_ms':plan['QUERY PLAN'][0]['Execution Time']}), flush=True)
        if args.generate:
            projects = (await service.projects(owner, 'America/Phoenix'))['projects']
            project = max(projects, key=lambda p: (p['lastActive'],p['sessions']))
            start = perf_counter()
            await request_docs(service, owner, project['id'])
            # Run the requested job; production worker can also claim it safely.
            for _ in range(15):
                await run_next(service)
                saved = await read_docs(service, owner, project['id'])
                if saved['state'] not in ('queued','running'):
                    break
                await asyncio.sleep(2)
            assert saved['state'] == 'ready' and saved['docs'], saved['error']
            docs = saved['docs']
            body = docs['skillMd'].split('---',2)[2].strip()
            assert len(body)<=500 and docs['evidenceCount']>0
            other = Insights(os.environ['DATABASE_URL'])
            await other.pool.open(wait=True)
            persisted = await read_docs(other, owner, project['id'])
            await other.pool.close()
            assert docs == persisted['docs']
            print(json.dumps({'grok_saved':True,'model':docs['model'],'generation_ms':round((perf_counter()-start)*1000,2),
                              'skill_codepoints':len(body),'project_chars':len(docs['projectMd']),
                              'evidence_prompts':docs['evidenceCount'],'survived_new_connection':True}), flush=True)
    finally:
        await service.pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email',required=True)
    parser.add_argument('--generate',action='store_true')
    asyncio.run(main(parser.parse_args()))
