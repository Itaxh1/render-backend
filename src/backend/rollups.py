"""Short claims and fenced publication; the expensive scan holds no dirty lock."""


async def claim(connection):
    return await (await connection.execute("""
        update public.rollup_dirty set claim_token=gen_random_uuid(),
          claimed_until=now()+interval '2 minutes'
        where (user_id,local_day)=(
          select user_id,local_day from public.rollup_dirty
          where claimed_until is null or claimed_until < now()
          order by (dirtied_at < now()-interval '5 minutes') desc,
                   case when dirtied_at >= now()-interval '5 minutes' then local_day end desc,
                   dirtied_at,user_id,local_day
          for update skip locked limit 1
        ) returning user_id,local_day,generation,claim_token
    """)).fetchone()


async def compute(connection, job):
    return await (await connection.execute("""
        select e.user_id,e.local_day,s.source,count(distinct e.session_id) sessions,
               count(*) filter(where e.type in ('user','agent','tool')) events,
               count(tc.id) tools,count(tc.id) filter(where tc.status='succeeded') succeeded,
               count(tc.id) filter(where tc.status='failed') failed
        from public.events e
        join public.sessions s on s.user_id=e.user_id and s.id=e.session_id
        left join public.tool_calls tc on tc.user_id=e.user_id and tc.event_id=e.id
        where e.user_id=%s and e.local_day=%s group by e.user_id,e.local_day,s.source
    """, (job['user_id'],job['local_day']))).fetchall()


async def publish(connection, job, rows):
    # Caller transaction keeps this tiny check+write atomic. A newer importer
    # either wins before this check or re-dirties immediately after our commit.
    current = await (await connection.execute("""
        select generation from public.rollup_dirty
        where user_id=%s and local_day=%s and claim_token=%s for update
    """, (job['user_id'],job['local_day'],job['claim_token']))).fetchone()
    if current is None:
        return False
    if current['generation'] != job['generation']:
        await connection.execute("""update public.rollup_dirty set claimed_until=null,claim_token=null
            where user_id=%s and local_day=%s and claim_token=%s""",
            (job['user_id'],job['local_day'],job['claim_token']))
        return False
    async with connection.pipeline():
        await connection.execute('delete from public.daily_rollups where user_id=%s and local_day=%s',
                                 (job['user_id'],job['local_day']))
        for row in rows:  # At most two products, not one statement per event.
            await connection.execute("""insert into public.daily_rollups
                (user_id,local_day,source,sessions,events,tools,succeeded,failed)
                values(%s,%s,%s,%s,%s,%s,%s,%s)""",
                tuple(row[key] for key in ('user_id','local_day','source','sessions','events','tools','succeeded','failed')))
        await connection.execute('delete from public.rollup_dirty where user_id=%s and local_day=%s and claim_token=%s',
                                 (job['user_id'],job['local_day'],job['claim_token']))
    return True
