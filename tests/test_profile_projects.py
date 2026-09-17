import asyncio
import json
from datetime import date, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi import HTTPException

from backend.insights import Insights
from backend.profile_stats import build_profile, streaks
from backend.project_docs import validate_docs, render_docs, request_docs, read_docs, run_next
from test_day_api import api, record, upload


def test_streaks_empty_and_incomplete_current_period():
    today = date(2026, 9, 17)
    assert streaks([], today) == (0, 0, None)
    days = [today - timedelta(days=i) for i in range(1, 15)]
    assert streaks(days, today)[:2] == (14, 14)
    assert streaks(days, today + timedelta(days=1))[0] == 0
    assert streaks([date(2025,12,29), date(2026,1,5)], date(2026,1,12), 7)[:2] == (2,2)


def test_empty_profile_has_no_invented_metrics():
    p, projects = build_profile(uuid4(), [], [], [], date(2026,9,17), 'UTC')
    assert p['activeDays'] == p['toolCalls'] == p['prompts'] == 0
    assert p['since'] is None and p['subagents'] is None and p['streak']['freezes'] is None
    assert len(p['weeks']) == 26 and not projects
    assert all(a['status'] != 'earned' for a in p['achievements'])


def packet():
    return {'id':str(uuid4()), 'name':'Test: "project"', 'sessions':1, 'inputRevision':'v1',
            'prompts':[{'id':'e1','day':'2026-09-17','text':'Use npm and verify the UI in a browser.'}]}


def test_grok_output_requires_real_quotes_and_unicode_limit():
    p = packet()
    raw = {'summary':'A UI project.', 'decisions':[{'text':'Use npm.', 'event_id':'e1','quote':'Use npm'}], 'skill':'😀'*500}
    result = validate_docs(raw, p)
    docs = render_docs(result, p, 'grok-4.3')
    assert len(docs['skillMd'].split('---')[2].strip()) == 500
    assert 'event e1' in docs['projectMd'] and docs['model'] == 'grok-4.3'
    raw['skill'] += '😀'
    with pytest.raises(ValueError): validate_docs(raw, p)
    raw['skill'] = 'Use npm.'
    raw['decisions'][0]['quote'] = 'All tests passed'
    with pytest.raises(ValueError): validate_docs(raw, p)
    raw['decisions'] = []
    raw['skill'] = 'xai-' + 'a'*40
    with pytest.raises(ValueError): validate_docs(raw, p)


def refresh(dsn, owner):
    async def run():
        service = Insights(dsn)
        await service.pool.open(wait=True)
        try:
            async with service.pool.connection() as c:
                row = await (await c.execute('select * from private.account_insights where user_id=%s', (owner,))).fetchone()
            await service.refresh(row)
        finally:
            await service.pool.close()
    asyncio.run(run())


def test_profile_api_authentic_counts_and_foreign_isolation(api, migrated_database):
    client, token, owner = api
    rows = [record(1), record(2), record(3,'tool'),record(4,'agent')]
    for r in rows: r['event']['project_name'] = 'Test project'
    rows[0]['event']['content_preview'] = '# AGENTS.md instructions <INSTRUCTIONS> setup'
    rows[1]['event']['content_preview'] = 'continue'  # Still a human prompt, even if not a useful title.
    upload(client, token, rows)
    assert client.get('/v1/profile?tz=America/Phoenix').status_code == 200
    refresh(migrated_database, owner)
    result = client.get('/v1/profile?tz=America/Phoenix').json()
    assert result['profile']['prompts'] == 1
    assert result['profile']['toolCalls'] == 1 and result['profile']['activeDays'] == 1
    assert result['profile']['hours'][0]['pct'] == 100
    assert result['profile']['projectsTotal'] == 1
    assert client.get('/v1/profile?tz=bad').status_code == 422
    assert client.get('/v1/profile',headers={'authorization':f'Bearer {token}'}).status_code == 401
    projects = client.get('/v1/projects?tz=America/Phoenix').json()
    assert projects['total'] == 1 and projects['projects'][0]['prompts'] == 1
    pid = projects['projects'][0]['id']
    assert client.get(f'/v1/projects/{pid}/docs',headers={'authorization':'Bearer other'}).status_code == 404
    assert client.get('/v1/projects',headers={'authorization':'Bearer other'}).json()['projects'] == []
    with psycopg.connect(migrated_database) as c:
        c.execute('delete from public.events where user_id=%s', (owner,))
    assert client.get('/v1/profile?tz=America/Phoenix').json()['profile'] is None


def test_project_job_persistence_retry_cancel_and_purge(api, migrated_database, monkeypatch):
    client, token, owner = api
    r = record(1)
    r['event'].update(project_name='Docs project', content_preview='Use npm and verify the UI in a browser.')
    upload(client, token, [r])
    client.get('/v1/profile')
    refresh(migrated_database, owner)
    pid = client.get('/v1/projects').json()['projects'][0]['id']
    calls = []
    mode = ['success']
    running_service = []
    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kw):
            calls.append(kw)
            if mode[0] == 'failure':
                raise RuntimeError('Simulated provider outage')
            if mode[0] == 'cancel':
                await request_docs(running_service[0], owner, pid, cancel=True)
            p = json.loads(kw['json']['messages'][1]['content'])
            output = {'summary':'A UI project.', 'decisions':[{'text':'Use npm.', 'event_id':p['prompts'][0]['id'],'quote':'Use npm'}], 'skill':'Use npm. Verify the UI in a browser.'}
            class Response:
                def raise_for_status(self): pass
                def json(self): return {'choices':[{'message':{'content':json.dumps(output)}}]}
            return Response()
    monkeypatch.setattr('backend.project_docs.httpx.AsyncClient', FakeClient)
    async def run():
        service = Insights(migrated_database)
        running_service.append(service)
        service.api_key = 'test-only'
        await service.pool.open(wait=True)
        try:
            assert (await request_docs(service, owner, pid))['state'] == 'queued'
            assert (await request_docs(service, owner, pid))['state'] == 'queued'
            assert await run_next(service)
            saved = await read_docs(service, owner, pid)
            assert saved['state'] == 'ready' and 'Use npm' in saved['docs']['projectMd'], saved
            assert len(calls) == 1
            # A new service reads the saved result, no provider call.
            again = Insights(migrated_database)
            await again.pool.open(wait=True)
            assert (await read_docs(again, owner, pid))['docs'] == saved['docs']
            await again.pool.close()
            await request_docs(service, owner, pid)
            await request_docs(service, owner, pid, cancel=True)
            assert not await run_next(service)
            assert (await read_docs(service, owner, pid))['docs'] == saved['docs']
            # A provider outage must retain the last successful artifacts.
            mode[0] = 'failure'
            await request_docs(service, owner, pid)
            assert await run_next(service)
            failed = await read_docs(service, owner, pid)
            assert failed['state'] == 'failed' and failed['docs'] == saved['docs']
            # Cancel during the model call: its late response cannot publish.
            mode[0] = 'cancel'
            await request_docs(service, owner, pid)
            assert await run_next(service)
            canceled = await read_docs(service, owner, pid)
            assert canceled['state'] == 'ready' and canceled['docs'] == saved['docs']
            async with service.pool.connection() as c:
                await c.execute("update private.project_documents set requests_count=20 where user_id=%s", (owner,))
            with pytest.raises(HTTPException) as limit:
                await request_docs(service, owner, pid)
            assert limit.value.status_code == 429
            async with service.pool.connection() as c:
                await c.execute('delete from public.events where user_id=%s',(owner,))
            assert (await read_docs(service, owner, pid))['docs'] is None
        finally:
            await service.pool.close()
    asyncio.run(run())


def test_new_tables_inaccessible_to_browser_roles(migrated_database):
    with psycopg.connect(migrated_database) as c:
        for table in ('account_insights','project_documents'):
            assert c.execute("select relrowsecurity from pg_class where oid=%s::regclass",('private.'+table,)).fetchone()[0]
            for role in ('anon','authenticated'):
                assert not c.execute('select has_table_privilege(%s,%s,%s)',(role,'private.'+table,'select')).fetchone()[0]
