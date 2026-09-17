"""Saved Profile/Projects reads. Background computation never blocks page requests."""
import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .db_pool import pool
from .profile_stats import build_profile

# Version rows survive event/session deletion. No raw-event scan in a GET.
VERSION = """select md5(coalesce(string_agg(local_day::text||':'||ribbon||':'||story,
                    ',' order by local_day),'')) revision,
                    coalesce(sum(purge),0)::bigint purge from public.day_versions where user_id=%s"""


class Insights:
    def __init__(self, database_url):
        self.pool = pool(database_url, maximum=2, waiting=8)
        self.api_key = None
        self.model = 'grok-4.3'

    async def open(self):
        await self.pool.open(wait=True)
        self.task = asyncio.create_task(self.loop(), name='profile-projects')

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        await self.pool.close()

    async def loop(self):
        from .project_docs import run_next
        while True:
            try:
                await self.refresh_next()
                if self.api_key:
                    await run_next(self)
            except Exception as error:
                logging.getLogger(__name__).warning('insights worker: %s', type(error).__name__)
            await asyncio.sleep(3)

    async def snapshot(self, user_id, tz):
        try:
            ZoneInfo(tz)
        except (ValueError, KeyError):
            raise HTTPException(422, 'Invalid timezone')
        async with self.pool.connection() as c:
            row = await (await c.execute(f'''select a.*,v.revision current_revision,v.purge current_purge
                from private.account_insights a cross join lateral ({VERSION}) v where a.user_id=%s''',
                (user_id, user_id))).fetchone()
            if row is None or row['timezone'] != tz:
                await c.execute("""insert into private.account_insights(user_id,timezone) values(%s,%s)
                    on conflict(user_id) do update set timezone=excluded.timezone,checked_at='1970-01-01'""", (user_id, tz))
            if row is None:
                version = await (await c.execute(VERSION, (user_id,))).fetchone()
                return {'profile':None,'projects':[],'generatedAt':None,'revision':None,
                        'purge':str(version['purge']),'updating':True,'error':None}
            version = {'revision':row['current_revision'],'purge':row['current_purge']}
        safe = row['purge_revision'] == version['purge']
        profile = row['profile'] if safe and row['profile'] and row['profile']['timezone'] == tz else None
        return {'profile': profile, 'projects': row['projects'] if safe else [],
                'generatedAt': row['computed_at'].isoformat() if row['computed_at'] else None,
                'revision': row['source_revision'], 'purge': str(version['purge']),
                'updating': not profile or row['source_revision'] != version['revision'] or profile['asOf'] != str(datetime.now(ZoneInfo(tz)).date()),
                'error': row['error']}

    async def projects(self, user_id, tz):
        snapshot = await self.snapshot(user_id, tz)
        async with self.pool.connection() as c:
            saved = await (await c.execute("""select project_id::text,state,input_revision,purge_revision,error,
                docs->>'generatedAt' generated_at from private.project_documents where user_id=%s""", (user_id,))).fetchall()
        by_id = {r['project_id']: r for r in saved}
        for p in snapshot['projects']:
            doc = by_id.get(p['id'])
            p.update(state=doc['state'] if doc else 'none',
                     hasDocs=bool(doc and doc['generated_at'] and str(doc['purge_revision']) == snapshot['purge']),
                     stale=bool(doc and doc['input_revision'] != p['inputRevision']),
                     error=doc['error'] if doc else None)
        snapshot.pop('profile')
        snapshot['total'] = len(snapshot['projects'])
        snapshot['generationAvailable'] = bool(self.api_key)
        return snapshot

    async def refresh_next(self):
        async with self.pool.connection() as c:
            # Refresh one account at a time, with a 30-second minimum between checks.
            row = await (await c.execute("""update private.account_insights set checked_at=now()
                where user_id=(select user_id from private.account_insights
                  where checked_at<now()-interval '30 seconds' order by checked_at for update skip locked limit 1)
                returning *""")).fetchone()
        if not row:
            return False
        try:
            return await self.refresh(row)
        except Exception:
            async with self.pool.connection() as c:
                await c.execute("update private.account_insights set error='Refresh delayed; saved data retained' where user_id=%s", (row['user_id'],))
            raise

    async def refresh(self, row):
        user_id, tz = row['user_id'], row['timezone']
        today = datetime.now(ZoneInfo(tz)).date()
        async with self.pool.connection() as c:
            await c.execute('set transaction isolation level repeatable read read only')
            await c.execute("set local statement_timeout='60s'")
            version = await (await c.execute(VERSION, (user_id,))).fetchone()
            if row['profile'] and row['source_revision'] == version['revision'] and row['purge_revision'] == version['purge'] and row['profile']['timezone'] == tz and row['profile']['asOf'] == str(today):
                return False
            sessions = await (await c.execute("""select id,source,project_name,summary_input_version
                from public.sessions where user_id=%s""", (user_id,))).fetchall()
            rows = await (await c.execute("""
                with samples as (
                  select e.session_id,e.local_day,s.source,e.type,
                    extract(hour from e.created_at at time zone %s) h,
                    (e.type='user' and coalesce(ltrim(e.content_preview),'') !~*
                      '^(#{1,6}[[:space:]]*)?((AGENTS|CLAUDE)\\.md[[:space:]]+instructions|<(instructions|environment_context|system-reminder|permissions instructions|turn_aborted)|<!--[[:space:]]*context7)') human
                  from public.events e join public.sessions s on s.user_id=e.user_id and s.id=e.session_id
                  where e.user_id=%s and e.type in ('user','agent','tool')
                ) select session_id,local_day,source,count(*) events,
                  count(*) filter(where type='tool') tools,count(*) filter(where human) prompts,
                  count(*) filter(where human and h>=5 and h<9) early,
                  count(*) filter(where human and h>=9 and h<18) daytime,
                  count(*) filter(where human and h>=18 and h<23) evening,
                  count(*) filter(where human and (h>=23 or h<5)) night
                  from samples group by session_id,local_day,source
                """, (tz, user_id))).fetchall()
            tools = await (await c.execute("""select tool_name,count(*) runs from public.tool_calls
                where user_id=%s group by tool_name order by runs desc,tool_name limit 8""", (user_id,))).fetchall()
        profile, projects = build_profile(user_id, rows, sessions, tools, today, tz)
        async with self.pool.connection() as c:
            # Publish a coherent saved snapshot, even during ongoing imports;
            # readers get 'updating'. Never publish across a deletion barrier.
            current = await (await c.execute(VERSION, (user_id,))).fetchone()
            if current['purge'] != version['purge']:
                return False
            await c.execute("""update private.account_insights set profile=%s,projects=%s,source_revision=%s,
                purge_revision=%s,computed_at=now(),error=null where user_id=%s and timezone=%s""",
                (Jsonb(profile), Jsonb(projects), version['revision'], version['purge'], user_id, tz))
        return True
