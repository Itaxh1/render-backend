from uuid import uuid4

import psycopg
from psycopg.rows import dict_row


def seed(connection, user=None):
    user = user or uuid4()
    connection.execute("insert into auth.users values(%s)", (user,))
    device = connection.execute("""insert into public.devices(user_id,name,platform,token_hash,token_expires_at)
        values(%s,'fixture','test',%s,now()+interval '1 day') returning id""", (user, uuid4().bytes * 2)).fetchone()["id"]
    session = connection.execute("""insert into public.sessions(user_id,device_id,source,source_session_id,
        started_at,last_event_at,started_day) values(%s,%s,'codex','fixture',now(),now(),'2026-09-16') returning id""",
        (user, device)).fetchone()["id"]
    events = []
    for seq, day in enumerate(['2026-09-15', '2026-09-16']):
        events.append(connection.execute("""insert into public.events(user_id,device_id,session_id,source_file_id,
          source_sequence,revision,payload_hash,type,created_at,local_day,content_preview)
          values(%s,%s,%s,%s,%s,1,%s,'user',%s::date,%s,'Synthetic prompt') returning id""",
          (user, device, session, 'a'*64, seq, b'a'*32, day, day)).fetchone()['id'])
    return user, device, session, events


def test_deleting_session_keeps_versions_and_records_tombstone(migrated_database):
    with psycopg.connect(migrated_database, row_factory=dict_row) as c:
        user, device, session, _ = seed(c)
        c.execute("delete from public.sessions where user_id=%s and id=%s", (user, session))
        rows = c.execute("select * from public.day_versions where user_id=%s order by local_day", (user,)).fetchall()
        assert len(rows) == 2
        assert all(r['purge'] == 1 and r['story_mutation'] == 1 for r in rows)
        assert c.execute("select count(*) n from private.deleted_sessions where user_id=%s", (user,)).fetchone()['n'] == 1
        assert c.execute("select count(*) n from public.dashboard_changes where user_id=%s and session_id is null", (user,)).fetchone()['n'] == 2


def test_event_delete_invalidates_all_summary_days_and_removes_derived_text(migrated_database):
    with psycopg.connect(migrated_database, row_factory=dict_row) as c:
        user, _, session, events = seed(c)
        c.execute("""insert into public.summaries(user_id,session_id,input_revision,tldr,outcome,model)
          values(%s,%s,1,'Synthetic summary','unknown','fixture')""", (user, session))
        c.execute("delete from public.events where user_id=%s and id=%s", (user, events[0]))
        assert c.execute("select count(*) n from public.day_versions where user_id=%s and purge=1", (user,)).fetchone()['n'] == 2
        assert c.execute("select count(*) n from public.summaries where user_id=%s", (user,)).fetchone()['n'] == 0
        assert c.execute("select summary_input_version v from public.sessions where id=%s", (session,)).fetchone()['v'] == 2


def test_delete_rollback_and_account_cascade(migrated_database):
    with psycopg.connect(migrated_database, row_factory=dict_row) as c:
        user, _, session, _ = seed(c)
        c.execute("savepoint before_delete")
        c.execute("delete from public.sessions where id=%s", (session,))
        c.execute("rollback to before_delete")
        assert c.execute("select count(*) n from public.day_versions where user_id=%s", (user,)).fetchone()['n'] == 0
        c.execute("delete from auth.users where id=%s", (user,))
        for table in ['public.day_versions', 'private.deleted_sessions', 'public.events']:
            assert c.execute(f"select count(*) n from {table} where user_id=%s", (user,)).fetchone()['n'] == 0


def test_version_reads_are_owner_scoped_and_browser_cannot_write(migrated_database):
    with psycopg.connect(migrated_database, row_factory=dict_row) as c:
        user, _, _, _ = seed(c)
        other, _, _, _ = seed(c)
        for owner in [user, other]:
            c.execute("select private.bump_day_versions(%s,array['2026-09-16'::date],true,true,true)", (owner,))
        c.execute("set local role authenticated")
        c.execute("select set_config('request.jwt.claim.sub',%s,true)", (str(user),))
        rows = c.execute("select user_id from public.day_versions").fetchall()
        assert rows == [{'user_id': user}]
        assert not c.execute("select has_table_privilege('public.day_versions','UPDATE') ok").fetchone()['ok']
