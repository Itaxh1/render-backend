"""Opaque cache tokens; database counters are never client ordering values."""
import hashlib
from datetime import date, timedelta

from .models import DayRevisions


def revision_token(generation, user_id, day, kind, version):
    material = f'{generation}:{user_id}:{day}:{kind}:{version}'
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def revisions(generation, user_id, day, row):
    return DayRevisions(**{
        kind: revision_token(generation, user_id, day, kind, row.get(kind, 0))
        for kind in ('ribbon', 'extras', 'story', 'purge')
    })


def year_revisions(generation, user_id, year, rows):
    by_day = {r['local_day']: r for r in rows}
    day, end = date(year, 1, 1), date(year + 1, 1, 1)
    result = {}
    while day < end:
        result[day.isoformat()] = revisions(generation, user_id, day, by_day.get(day, {})).model_dump()
        day += timedelta(days=1)
    return result


async def bump_session_extras(connection, user_id, session_id):
    await connection.execute("""
        select private.bump_day_versions(%s,
          array(select distinct local_day from public.events
                where user_id=%s and session_id=%s order by local_day), false,true,false)
    """, (user_id, user_id, session_id))
