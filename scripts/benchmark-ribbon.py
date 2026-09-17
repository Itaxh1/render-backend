"""Read-only production/staging parity and EXPLAIN check via linked Supabase CLI.

Print counts and timings only, never transcript content or credentials.
Usage: .venv/bin/python scripts/benchmark-ribbon.py --user-id UUID --day YYYY-MM-DD
"""
import argparse
from datetime import date
import json
import subprocess
from uuid import UUID

from psycopg import sql

from backend.day_queries import RIBBON_EVENTS_SQL


LEGACY = """
select e.id, e.created_at, e.local_day, s.source,
       e.session_id, e.type, tc.status, tc.tool_name, tc.duration_ms
from public.events e
join public.sessions s on s.user_id = e.user_id and s.id = e.session_id
left join public.tool_calls tc on tc.user_id = e.user_id and tc.event_id = e.id
where e.user_id = %s and e.local_day = %s and e.type in ('user','agent','tool')
order by e.created_at, e.id
"""


def bind(query, values):
    parts = query.split('%s')
    assert len(parts) == len(values) + 1
    return ''.join(part + sql.Literal(value).as_string()
                   for part, value in zip(parts, values)) + parts[-1]


def run(query):
    guarded = "begin read only; set local statement_timeout = '20s'; " + query + "; rollback;"
    result = subprocess.run(
        ['npx', '--no-install', 'supabase', 'db', 'query', '--linked', guarded, '--output', 'json'],
        text=True, capture_output=True, timeout=45,
    )
    if result.returncode:
        raise RuntimeError('Read-only benchmark failed; check connectivity or statement timeout')
    return json.loads(result.stdout)['rows']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', required=True, type=UUID)
    parser.add_argument('--day', required=True, type=date.fromisoformat)
    args = parser.parse_args()
    old = bind(LEGACY, [args.user_id, args.day])
    new = bind(RIBBON_EVENTS_SQL, [args.user_id, args.day, args.user_id])
    for label, query in [('legacy', old), ('bounded', new)]:
        plan = run('explain (analyze, buffers, format json) ' + query)[0]['QUERY PLAN'][0]
        print(json.dumps({'day': str(args.day), 'query': label,
                          'execution_ms': plan['Execution Time'],
                          'rows': plan['Plan']['Actual Rows']}), flush=True)
    differences = run(f'''with old as materialized ({old}), new as materialized ({new})
      select count(*) as differences from (
        (select * from old except all select * from new)
        union all (select * from new except all select * from old)
      ) diff''')[0]['differences']
    print(json.dumps({'day': str(args.day), 'multiset_differences': differences}), flush=True)
    if differences:
        raise SystemExit(1)
