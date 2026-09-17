"""Independent day tiers. No summary generation or prompt text on ribbon reads."""
from contextlib import asynccontextmanager
from datetime import timedelta
import time

from psycopg import errors

from .day_contract import (
    AnalysisState, ChangedStoryCursor, DayExtrasResponse, DayRibbonResponse,
    DayStoryResponse, ExtrasSections, MissingDaySession, SessionHeader,
    SessionSummary, StoryRow, day_axis,
)
from .day_queries import RIBBON_EVENTS_SQL, RIBBON_SESSIONS_SQL, SUMMARY_ROWS_SQL, TOKEN_ROWS_SQL
from .day_versions import revisions, revision_token
from .models import DashboardEvent
from .story_cursor import decode_cursor, encode_cursor
from .usage import aggregate_usage


def milliseconds(value):
    return int(value.timestamp() * 1000)


class DayReads:
    @asynccontextmanager
    async def _day_snapshot(self, user_id, day):
        async with self._pool.connection() as c:
            async with c.transaction():
                await c.execute('set transaction isolation level repeatable read read only')
                await c.execute("set local statement_timeout='10s'; set local lock_timeout='2s'")
                meta = await (await c.execute("""
                    select a.generation,a.cursor_key,transaction_timestamp() generated,
                           coalesce(d.ribbon,0) ribbon,coalesce(d.extras,0) extras,
                           coalesce(d.story,0) story,coalesce(d.purge,0) purge,
                           coalesce(d.story_mutation,0) story_mutation
                    from private.day_api_state a
                    left join public.day_versions d on d.user_id=%s and d.local_day=%s
                    where a.singleton
                """, (user_id, day))).fetchone()
                versions = revisions(meta['generation'], user_id, day, meta)
                yield c, meta, versions

    async def day_ribbon(self, user_id, day, tz='UTC'):
        axis = day_axis(day, tz)
        async with self._day_snapshot(user_id, day) as (c, meta, versions):
            headers = await (await c.execute(RIBBON_SESSIONS_SQL, (user_id, day, user_id))).fetchall()
            rows = await (await c.execute(RIBBON_EVENTS_SQL, (user_id, day, user_id))).fetchall()
        events = [DashboardEvent(
            id=str(r['id']), t=milliseconds(r['created_at']), d=r['local_day'],
            src=r['source'], s=str(r['session_id']), k=r['type'],
            st=(r['status'] or 'unknown') if r['type'] == 'tool' else 'unknown',
            n=r['tool_name'] if r['type'] == 'tool' else None,
            ms=r['duration_ms'] if r['type'] == 'tool' and r['source'] == 'claude-code' else None,
        ) for r in rows]
        return DayRibbonResponse(
            date=day, generated=meta['generated'], revision=versions.ribbon,
            purge_revision=versions.purge, axis=axis,
            off_axis_event_ids=[e.id for e in events if not axis.start_ms <= e.t < axis.end_ms],
            sessions=[SessionHeader(
                id=str(r['id']), src=r['source'], title=r['title'], proj=r['project_name'],
                model=r['model'], start=milliseconds(r['started_at']), end=milliseconds(r['ended_at']),
                d=day, day_first_ms=milliseconds(r['day_first']), day_last_ms=milliseconds(r['day_last']),
            ) for r in headers], events=events,
        )

    async def day_extras(self, user_id, day):
        sections = ExtrasSections()
        summaries, analysis = {}, {}
        tokens, tokens_by_source, usage_only = {}, {}, None
        async with self._day_snapshot(user_id, day) as (c, meta, versions):
            # Savepoints let one timed-out section fail without marking the
            # other one empty or poisoning the shared read-only snapshot.
            try:
                async with c.transaction():
                    rows = await (await c.execute(SUMMARY_ROWS_SQL, (user_id, day, user_id))).fetchall()
                    usage_only = sum(not r['visible'] for r in rows)
                    for r in rows:
                        if not r['visible']:
                            continue
                        pending = r['job_status'] in ('pending', 'processing')
                        failed = r['job_status'] == 'failed'
                        has_summary = r['tldr'] is not None
                        summaries[str(r['id'])] = SessionSummary(
                            summary=r['tldr'],
                            summary_state='ready' if has_summary else 'pending' if pending else 'failed' if failed else 'not_requested',
                            refresh_state='pending' if pending else 'failed' if failed else 'idle',
                            generated_at=r['generated_at'], model=r['model'],
                            input_revision=str(r['input_revision']) if has_summary else None,
                            is_stale=has_summary and r['input_revision'] != r['summary_input_version'],
                        )
                        analysis[str(r['id'])] = AnalysisState(state='unsupported', input_revision=None)
            except errors.QueryCanceled:
                sections.summaries = 'failed'
            try:
                async with c.transaction():
                    end = day + timedelta(days=1)
                    token_rows = await (await c.execute(TOKEN_ROWS_SQL, (user_id, day, end, user_id, day, end))).fetchall()
                    tokens, tokens_by_source = aggregate_usage(token_rows)
            except errors.QueryCanceled:
                sections.tokens = 'failed'
        return DayExtrasResponse(
            date=day, generated=meta['generated'], revision=versions.extras,
            ribbon_revision=versions.ribbon, purge_revision=versions.purge,
            sections=sections, summaries_by_session=summaries, tokens=tokens,
            tokens_by_source=tokens_by_source, usage_only_session_count=usage_only,
            analysis_by_session=analysis,
        )

    async def day_story(self, user_id, day, session_id=None, cursor=None, limit=200):
        async with self._day_snapshot(user_id, day) as (c, meta, versions):
            mutation = revision_token(meta['generation'], user_id, day, 'mutation', meta['story_mutation'])
            state = decode_cursor(cursor, meta['cursor_key'], user_id, day, session_id) if cursor else None
            if state and (state['p'] != versions.purge or state['m'] != mutation):
                raise ChangedStoryCursor('Story changed; restart pagination')
            if session_id is not None:
                found = await (await c.execute("select 1 from public.sessions where user_id=%s and id=%s",
                    (user_id, int(session_id)))).fetchone()
                if not found:
                    raise MissingDaySession('Session not found')
            # Keep the query indexable: the optional predicate is literal SQL,
            # never concatenated caller input.
            where = "e.user_id=%s and e.local_day=%s and e.type in ('user','agent') and e.content_preview is not null"
            params = [user_id, day]
            if session_id is not None:
                where += ' and e.session_id=%s'
                params.append(int(session_id))
            if state is None:
                bounds = await (await c.execute(f"""
                    select max(e.id) high_id,
                      (array_agg(e.created_at order by e.created_at desc,e.id desc))[1] high_t,
                      (array_agg(e.id order by e.created_at desc,e.id desc))[1] high_key_id
                    from public.events e where {where}
                """, params)).fetchone()
                state = dict(v=1, u=str(user_id), d=day.isoformat(), s=session_id,
                    exp=int(time.time())+900, p=versions.purge, m=mutation, r=versions.story,
                    hi=bounds['high_id'], ht=bounds['high_t'].isoformat() if bounds['high_t'] else None,
                    hk=bounds['high_key_id'], last=None)
            rows = []
            if state['hi'] is not None:
                where += ' and e.id <= %s and (e.created_at,e.id) <= (%s::timestamptz,%s)'
                params += [state['hi'], state['ht'], state['hk']]
                if state['last'] is not None:
                    where += ' and (e.created_at,e.id) > (%s::timestamptz,%s)'
                    params += state['last']
                rows = await (await c.execute(f"""
                    select e.id,e.created_at,e.local_day,e.session_id,s.source,
                           e.type,e.content_preview,e.truncated
                    from public.events e join public.sessions s on s.user_id=e.user_id and s.id=e.session_id
                    where {where} order by e.created_at,e.id limit %s
                """, [*params, limit+1])).fetchall()
            next_cursor = None
            if len(rows) > limit:
                rows = rows[:limit]
                last = rows[-1]
                state['last'] = [last['created_at'].isoformat(), last['id']]
                next_cursor = encode_cursor(state, meta['cursor_key'])
        return DayStoryResponse(
            date=day, generated=meta['generated'], revision=versions.story,
            series_revision=state['r'], purge_revision=versions.purge,
            has_newer_data=versions.story != state['r'], next_cursor=next_cursor,
            story=[StoryRow(id=str(r['id']), t=milliseconds(r['created_at']), d=r['local_day'],
                s=str(r['session_id']), src=r['source'], k=r['type'], x=r['content_preview'],
                truncated=r['truncated']) for r in rows],
        )
