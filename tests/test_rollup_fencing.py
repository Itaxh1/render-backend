import asyncio

import psycopg
from psycopg.rows import dict_row

from backend import rollups
from test_day_versions import seed


def test_rollup_rejects_stale_compute_and_preserves_new_dirty_work(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        user,_,_,_ = seed(c)
        c.execute("insert into public.rollup_dirty(user_id,local_day,dirtied_at) values(%s,'2026-09-16','1970-01-01')",(user,))
    async def run():
        async with await psycopg.AsyncConnection.connect(migrated_database,row_factory=dict_row) as worker:
            async with worker.transaction():
                job = await rollups.claim(worker)
            async with worker.transaction():
                rows = await rollups.compute(worker,job)
            # A separate writer can bump the dirty generation while a worker
            # computes. Publication must not clear that notification.
            async with await psycopg.AsyncConnection.connect(migrated_database) as writer:
                await writer.execute("set local lock_timeout='100ms'")
                await writer.execute('update public.rollup_dirty set generation=generation+1 where user_id=%s',(user,))
            async with worker.transaction():
                assert not await rollups.publish(worker,job,rows)
            async with worker.transaction():
                newer = await rollups.claim(worker)
                assert newer['generation'] == job['generation']+1
            async with worker.transaction():
                rows = await rollups.compute(worker,newer)
            async with worker.transaction():
                assert await rollups.publish(worker,newer,rows)
    asyncio.run(run())
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        assert c.execute('select count(*) n from public.rollup_dirty where user_id=%s',(user,)).fetchone()['n'] == 0
        assert c.execute('select events from public.daily_rollups where user_id=%s',(user,)).fetchone()['events'] == 1
        c.execute('delete from auth.users where id=%s',(user,))


def test_expired_rollup_lease_cannot_overwrite_newer_worker(migrated_database):
    with psycopg.connect(migrated_database,row_factory=dict_row) as c:
        user,_,_,_ = seed(c)
        c.execute("insert into public.rollup_dirty(user_id,local_day,dirtied_at) values(%s,'2026-09-16','1970-01-01')",(user,))
    async def run():
        async with await psycopg.AsyncConnection.connect(migrated_database,row_factory=dict_row) as c:
            async with c.transaction():
                old = await rollups.claim(c)
            async with c.transaction():
                await c.execute("update public.rollup_dirty set claimed_until=now()-interval '1 minute' where user_id=%s",(user,))
            async with c.transaction():
                current = await rollups.claim(c)
            assert current['claim_token'] != old['claim_token']
            async with c.transaction():
                assert not await rollups.publish(c,old,[])
            async with c.transaction():
                rows = await rollups.compute(c,current)
            async with c.transaction():
                assert await rollups.publish(c,current,rows)
    asyncio.run(run())
    with psycopg.connect(migrated_database) as c:
        c.execute('delete from auth.users where id=%s',(user,))
