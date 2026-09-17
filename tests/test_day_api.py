import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row
import pytest
import threading

from backend.app import create_app
from backend.config import Settings, WorkerSettings
from backend.day_contract import day_axis
from backend.postgres import PostgresStore
from backend.day_reads import DayReads
from backend.worker import SummaryOutput, SummaryWorker


@pytest.fixture
def api(migrated_database):
    owner, other = uuid4(), uuid4()
    with psycopg.connect(migrated_database) as c:
        c.execute('insert into auth.users values(%s),(%s)', (owner, other))
    class Verifier:
        async def verify(self, token):
            if token == 'owner':
                return owner
            if token == 'other':
                return other
            raise HTTPException(401, 'Invalid token')
    settings = Settings(database_url=migrated_database, supabase_url='https://example.supabase.co',
                        rexy_web_origin='http://localhost:5173')
    store = PostgresStore(migrated_database)
    with TestClient(create_app(settings, store, Verifier()), headers={'authorization': 'Bearer owner'}) as client:
        claim = client.post('/v1/install/claims').json()['claim_token']
        result = client.post('/v1/devices/exchange-claim', json={
            'claim_token': claim, 'device_name': 'Synthetic device', 'platform': 'test'}).json()
        yield client, result['device_token'], owner


def record(seq, kind='user', day='2026-09-16', session='synthetic', revision=1):
    event = dict(session_id=session, type=kind, created_at=f'{day}T12:{seq % 60:02d}:00Z',
                 local_day=day, content_preview='Synthetic prompt' if kind in ('user','agent') else None)
    if kind == 'tool':
        event.update(tool_name='Read',tool_status='succeeded',source_call_id=f'call-{seq}',duration_ms=100)
    return dict(source='codex',source_file_id='a'*64,sequence=seq,revision=revision,payload_hash='b'*64,event=event)


def upload(client, token, records, sequence=1):
    response = client.post('/v1/ingest/batches',headers={'authorization': f'Bearer {token}'},json={
        'protocol_version': 1,'batch_id': str(uuid4()),'device_sequence': sequence,'extractor_version': 1,'records': records})
    assert response.status_code == 200, response.text
    return response.json()


def test_ribbon_and_extras_contract_isolated_and_empty_day_stable(api):
    client, token, owner = api
    upload(client, token, [record(1), record(2,'agent'), record(3,'tool')])
    response = client.get('/v1/day/ribbon?date=2026-09-16&tz=America/Phoenix')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    ribbon = response.json()
    assert ribbon['snapshot_complete'] is True
    assert len(ribbon['sessions']) == 1 and len(ribbon['events']) == 3
    assert ribbon['sessions'][0]['title'] == 'Synthetic prompt'
    assert [e['st'] for e in ribbon['events']] == ['unknown','unknown','succeeded']
    assert all(e['ms'] is None for e in ribbon['events'])  # Codex durations remain unknown.
    assert all('x' not in e and 'content_preview' not in e for e in ribbon['events'])
    assert ribbon['markers_state'] == 'unsupported'
    extras = client.get('/v1/day/extras?date=2026-09-16').json()
    assert extras['ribbon_revision'] == ribbon['revision']
    assert extras['purge_revision'] == ribbon['purge_revision']
    calendar = client.get('/v1/calendar?year=2026').json()
    assert len(calendar['day_revisions']) == 365
    assert calendar['day_revisions']['2026-09-16']['ribbon'] == ribbon['revision']
    foreign = client.get('/v1/day/ribbon?date=2026-09-16',headers={'authorization':'Bearer other'}).json()
    assert foreign['events'] == [] and foreign['revision'] != ribbon['revision']
    empty = client.get('/v1/day/ribbon?date=2026-01-01').json()
    assert empty['revision'] == client.get('/v1/day/ribbon?date=2026-01-01').json()['revision']
    assert client.get('/v1/day/ribbon?date=2026-09-16&tz=Not/AZone').status_code == 422
    assert client.get('/v1/day/ribbon?date=2026-09-16',headers={'authorization':f'Bearer {token}'}).status_code == 401
    assert client.get('/v1/day?date=2026-09-16').status_code == 200  # Legacy client unchanged.


def test_story_traversal_survives_appends_but_not_edits(api):
    client, token, _ = api
    upload(client, token, [record(1),record(2),record(3)])
    first = client.get('/v1/day/story?date=2026-09-16&limit=1').json()
    assert first['next_cursor'] and len(first['story']) == 1
    # A late historical insert is outside this traversal's ID fence.
    extra = record(4)
    extra['event']['created_at'] = '2026-09-16T12:01:30Z'
    upload(client, token, [extra],2)
    second = client.get('/v1/day/story', params={'date':'2026-09-16','limit':1,'cursor':first['next_cursor']})
    assert second.status_code == 200, second.text
    second = second.json()
    assert second['has_newer_data'] and second['series_revision'] == first['series_revision']
    assert second['story'][0]['id'] != first['story'][0]['id']
    foreign = client.get('/v1/day/story',params={'date':'2026-09-16','cursor':first['next_cursor']},
                         headers={'authorization':'Bearer other'})
    assert foreign.status_code == 400
    assert client.get('/v1/day/story',params={'date':'2026-09-15','cursor':first['next_cursor']}).status_code == 400
    assert client.get('/v1/day/story?date=2026-09-16&cursor=tampered').status_code == 400
    updated = record(2, revision=2)
    updated['event']['content_preview'] = 'Corrected synthetic prompt'
    upload(client, token, [updated],3)
    conflict = client.get('/v1/day/story',params={'date':'2026-09-16','cursor':second['next_cursor']})
    assert conflict.status_code == 409


def test_late_result_invalidates_invocation_and_multiday_headers(api):
    client, token, _ = api
    upload(client, token, [record(1,'tool',day='2026-09-15'),record(2,day='2026-09-16')])
    before = client.get('/v1/day/ribbon?date=2026-09-15').json()
    result = record(3,'tool_result')
    result['event'].update(source_call_id='call-1',tool_status='failed')
    upload(client, token, [result],2)
    after = client.get('/v1/day/ribbon?date=2026-09-15').json()
    assert after['revision'] != before['revision']
    assert len(after['events']) == 1 and after['events'][0]['st'] == 'failed'
    assert after['events'][0]['ms'] is None
    receipt = upload(client, token, [result],3)
    assert receipt['duplicate'] == 1
    assert client.get('/v1/day/ribbon?date=2026-09-15').json()['revision'] == after['revision']


def test_delete_purge_and_stale_device_replay(api,migrated_database):
    client, token, owner = api
    upload(client, token, [record(1),record(2)])
    old = client.get('/v1/day/ribbon?date=2026-09-16').json()
    first = client.get('/v1/day/story?date=2026-09-16&limit=1').json()
    with psycopg.connect(migrated_database) as c:
        c.execute('delete from public.sessions where user_id=%s', (owner,))
    new = client.get('/v1/day/ribbon?date=2026-09-16').json()
    assert new['events'] == [] and new['purge_revision'] != old['purge_revision']
    assert client.get('/v1/day/story',params={'date':'2026-09-16','cursor':first['next_cursor']}).status_code == 409
    assert upload(client, token, [record(1,revision=2)],2)['duplicate'] == 1
    assert client.get('/v1/day/ribbon?date=2026-09-16').json()['events'] == []


def test_exact_seven_day_automatic_summary_window_and_explicit_older_request(api,migrated_database):
    client, token, owner = api
    today = datetime.now(timezone.utc).date()
    days = [today-timedelta(days=7),today-timedelta(days=6),today,today+timedelta(days=1)]
    upload(client, token, [record(i,day=d.isoformat(),session=f'session-{i}') for i,d in enumerate(days)])
    with psycopg.connect(migrated_database, row_factory=dict_row) as c:
        rows = c.execute('select s.started_day from private.summary_jobs j join public.sessions s on s.id=j.session_id where j.user_id=%s',(owner,)).fetchall()
        assert sorted(r['started_day'] for r in rows) == days[1:3]
        old = c.execute("select id from public.sessions where user_id=%s and source_session_id='session-0'",(owner,)).fetchone()['id']
    assert client.post(f'/v1/sessions/{old}/summaries').status_code == 202


def test_usage_deltas_keep_previous_day_and_usage_only_sessions(api):
    client, token, _ = api
    old = record(1,'usage',day='2026-09-15')
    new = record(2,'usage')
    for r, value in [(old,100),(new,140)]:
        r['event'].update(token_input=value,token_output=10,usage_cumulative=True)
    upload(client, token, [old,new])
    assert client.get('/v1/day/ribbon?date=2026-09-16').json()['events'] == []
    extras = client.get('/v1/day/extras?date=2026-09-16').json()
    assert extras['usage_only_session_count'] == 1
    assert extras['tokens_by_source']['2026-09-16']['codex']['total'] == 40


def test_snapshot_axis_covers_dst_and_retains_off_axis_events(api):
    assert day_axis(date(2026,3,8),'America/New_York').end_ms - day_axis(date(2026,3,8),'America/New_York').start_ms == 23*3600000
    assert day_axis(date(2026,11,1),'America/New_York').end_ms - day_axis(date(2026,11,1),'America/New_York').start_ms == 25*3600000
    client, token, _ = api
    r = record(1)
    r['event']['created_at'] = '2026-09-16T01:00:00Z'
    upload(client,token,[r])
    ribbon = client.get('/v1/day/ribbon?date=2026-09-16&tz=America/Phoenix').json()
    assert ribbon['off_axis_event_ids'] == [ribbon['events'][0]['id']]


def test_summary_lease_prevents_stale_completion_and_saved_text_survives_refresh(api,migrated_database):
    client, token, owner = api
    day = datetime.now(timezone.utc).date().isoformat()
    upload(client,token,[record(1,day=day)])
    sid = client.get('/v1/day/ribbon',params={'date':day}).json()['sessions'][0]['id']
    client.post(f'/v1/sessions/{sid}/summaries')
    with psycopg.connect(migrated_database) as c:
        # Isolate this worker test from other users' synthetic jobs.
        c.execute("update private.summary_jobs set available_at=now()+interval '1 day' where user_id<>%s",(owner,))
        c.execute('update private.summary_jobs set available_at=now() where user_id=%s',(owner,))
    async def run():
        worker = SummaryWorker(WorkerSettings(migrated_database,'unused'))
        await worker.open()
        try:
            job = await worker.claim()
            assert job is not None and job.session_id == int(sid)
            facts = await worker.facts(job)
            assert facts['goal'] == 'Synthetic prompt'
            await worker.complete(job,SummaryOutput(tldr='Saved synthetic summary.',outcome='partial'))
            await worker.complete(job,SummaryOutput(tldr='Must not overwrite.',outcome='completed'))
        finally:
            await worker.close()
    asyncio.run(run())
    old = client.get('/v1/day/extras',params={'date':day}).json()
    assert old['summaries_by_session'][sid]['summary'] == 'Saved synthetic summary.'
    upload(client,token,[record(2,day=day)],2)
    new = client.get('/v1/day/extras',params={'date':day}).json()
    assert new['summaries_by_session'][sid]['summary'] == 'Saved synthetic summary.'
    assert new['summaries_by_session'][sid]['is_stale'] is True
    assert new['summaries_by_session'][sid]['refresh_state'] == 'pending'


def test_summary_stale_lease_and_delete_during_generation_cannot_publish(api,migrated_database):
    client,token,owner = api
    day = datetime.now(timezone.utc).date().isoformat()
    upload(client,token,[record(1,day=day)])
    with psycopg.connect(migrated_database) as c:
        c.execute("update private.summary_jobs set available_at=now()+interval '1 day' where user_id<>%s",(owner,))
        c.execute('update private.summary_jobs set available_at=now() where user_id=%s',(owner,))
    async def run():
        worker = SummaryWorker(WorkerSettings(migrated_database,'unused'))
        await worker.open()
        try:
            old = await worker.claim()
            async with await psycopg.AsyncConnection.connect(migrated_database) as c:
                await c.execute("update private.summary_jobs set locked_at=now()-interval '11 minutes' where user_id=%s",(owner,))
            current = await worker.claim()
            assert current.lease_token != old.lease_token
            result = SummaryOutput(tldr='Must not be published.',outcome='unknown')
            await worker.complete(old,result)
            async with await psycopg.AsyncConnection.connect(migrated_database,row_factory=dict_row) as c:
                count = await (await c.execute('select count(*) n from public.summaries where user_id=%s',(owner,))).fetchone()
                assert count['n'] == 0
                await c.execute('delete from public.sessions where user_id=%s',(owner,))
            await worker.complete(current,result)
        finally:
            await worker.close()
    asyncio.run(run())
    assert client.get('/v1/day/extras',params={'date':day}).json()['summaries_by_session'] == {}


def test_revision_and_rows_share_one_snapshot_during_concurrent_write(api,migrated_database,monkeypatch):
    client,token,owner = api
    upload(client,token,[record(1)])
    before = client.get('/v1/day/ribbon?date=2026-09-16').json()
    started,release = threading.Event(),threading.Event()
    original = DayReads._day_snapshot
    @asynccontextmanager
    async def held(self,*args):
        async with original(self,*args) as state:
            started.set()
            assert await asyncio.to_thread(release.wait,3)
            yield state
    monkeypatch.setattr(DayReads,'_day_snapshot',held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.get,'/v1/day/ribbon?date=2026-09-16')
        try:
            assert started.wait(2)
            with psycopg.connect(migrated_database) as c:
                c.execute("""insert into public.events(user_id,device_id,session_id,source_file_id,source_sequence,
                    revision,payload_hash,type,created_at,local_day,content_preview)
                    select user_id,device_id,session_id,source_file_id,999,1,payload_hash,'agent',created_at,local_day,'Concurrent text'
                    from public.events where user_id=%s limit 1""",(owner,))
                c.execute("select private.bump_day_versions(%s,array['2026-09-16'::date],true,true,true)",(owner,))
        finally:
            release.set()
        snapshot = future.result().json()
    assert snapshot['revision'] == before['revision']
    assert snapshot['events'] == before['events']
    monkeypatch.setattr(DayReads,'_day_snapshot',original)
    after = client.get('/v1/day/ribbon?date=2026-09-16').json()
    assert after['revision'] != before['revision'] and len(after['events']) == 2


def test_failed_extras_section_does_not_erase_successful_tokens(api,monkeypatch):
    client,token,_ = api
    usage = record(1,'usage')
    usage['event'].update(token_input=10,token_output=2)
    upload(client,token,[usage])
    original = DayReads._day_snapshot
    @asynccontextmanager
    async def short_timeout(self,*args):
        async with original(self,*args) as state:
            await state[0].execute("set local statement_timeout='30ms'")
            yield state
    monkeypatch.setattr(DayReads,'_day_snapshot',short_timeout)
    monkeypatch.setattr('backend.day_reads.SUMMARY_ROWS_SQL',
        'select pg_sleep(0.1) where %s::uuid is not null and %s::date is not null and %s::uuid is not null')
    response = client.get('/v1/day/extras?date=2026-09-16')
    assert response.status_code == 200, response.text
    extras = response.json()
    assert extras['sections'] == {'summaries':'failed','tokens':'ready','findings':'unsupported'}
    assert extras['tokens_by_source']['2026-09-16']['codex']['total'] == 12
