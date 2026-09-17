"""Deterministic, inexpensive presentation over a saved account aggregate."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5


def project_id(user_id, name):
    # Existing clients provide folder labels, not repository identities.
    return str(uuid5(NAMESPACE_URL, f'rexy:project-label:{user_id}:{name}'))


def streaks(days, today, step=1):
    days = sorted(set(days))
    best, run, start, best_range = 0, 0, None, None
    previous = None
    for day in days:
        run = run + 1 if previous and day == previous + timedelta(days=step) else 1
        if run == 1:
            start = day
        if run > best:
            best, best_range = run, [start.isoformat(), day.isoformat()]
        previous = day
    current = 0
    cursor = today if today in days else today - timedelta(days=step)
    day_set = set(days)
    while cursor in day_set:
        current += 1
        cursor -= timedelta(days=step)
    return current, best, best_range


def build_profile(user_id, rows, sessions, tools, today, timezone):
    days, hours, by_project = {}, Counter(), defaultdict(list)
    session_map = {s['id']: s for s in sessions}
    for r in rows:
        d = r['local_day']
        day = days.setdefault(d, {'events': 0, 'tools': 0, 'prompts': 0, 'sources': set()})
        for k in ('events', 'tools', 'prompts'):
            day[k] += r[k]
        if r['events']:
            day['sources'].add(r['source'])
        for k in ('early', 'daytime', 'evening', 'night'):
            hours[k] += r[k]
        name = (session_map[r['session_id']]['project_name'] or '').strip()
        if name:
            by_project[name].append(r)
    active = sorted(d for d, r in days.items() if r['events'] and d <= today)
    daily, best_daily, best_range = streaks(active, today)
    weekly = Counter(d - timedelta(days=d.weekday()) for d in active)
    monday = today - timedelta(days=today.weekday())
    current_weekly, best_weekly, _ = streaks([d for d, n in weekly.items() if n >= 3], monday, 7)
    projects = []
    for name, group in by_project.items():
        ids = sorted({r['session_id'] for r in group})
        dates = sorted({r['local_day'] for r in group if r['events']})
        if not dates:
            continue
        revision = sha256('|'.join(f'{i}:{session_map[i]["summary_input_version"]}' for i in ids).encode()).hexdigest()
        sources = {session_map[i]['source'] for i in ids}
        projects.append({'id': project_id(user_id, name), 'name': name, 'sessions': len(ids),
                         'days': len(dates), 'prompts': sum(r['prompts'] for r in group),
                         'firstActive': str(dates[0]), 'lastActive': str(dates[-1]),
                         'agents': ' + '.join(label for src, label in [('claude-code', 'Claude Code'), ('codex', 'Codex')] if src in sources),
                         'inputRevision': revision})
    projects.sort(key=lambda p: (-p['days'], p['name']))
    prompts = sum(r['prompts'] for r in days.values())
    tool_count = sum(r['tools'] for r in days.values())
    busiest = max(active, key=lambda d: days[d]['tools']) if active else None
    peak = days[busiest]['tools'] if busiest else 0
    def badge(id, name, rule, value=None, target=None):
        return dict(id=id, name=name, rule=rule, **({} if value is None else {'value': value, 'target': target}),
                    status='untracked' if value is None else 'earned' if value >= target else 'progress')
    badges = [badge('fire', 'On Fire', '14-day daily streak', best_daily, 14),
              badge('steady', 'Steady', '8 weeks in a row with 3+ active days', best_weekly, 8),
              badge('comeback', 'Comeback Kid', '5 verified tests or builds red to green in a day'),
              badge('bigday', 'Big Day', '2,000 tool calls in one day', peak, 2000),
              badge('owl', 'Night Owl', '25% of prompts between 11 PM and 5 AM', round(hours['night'] / prompts * 100) if prompts else 0, 25),
              badge('early', 'Early Bird', 'A session before 6 AM'),
              badge('club', '50K Club', '50,000 tool calls', tool_count, 50000),
              badge('clean', 'Clean Run', '50+ verified actions, zero failures, one session'),
              badge('quick', 'Quick Draw', 'Verified red to green within five minutes'),
              badge('tag', 'Tag Team', 'Claude Code and Codex on the same day', sum(len(r['sources']) == 2 for r in days.values()), 1)]
    profile = dict(since=str(active[0]) if active else None, asOf=str(today), activeDays=len(active),
                   prompts=prompts, toolCalls=tool_count, subagents=None, subagentSessions=None,
                   projectsTotal=len(projects), projectsActive=sum(p['days'] >= 5 for p in projects),
                   timezone=timezone, coverage='Imported events; project groups use recorded folder labels. Sub-agent lineage and verified build recoveries are not available.',
                   streak=dict(weeklyCurrent=current_weekly, weeklyBest=best_weekly, dailyCurrent=daily,
                               dailyBest=best_daily, dailyBestRange=best_range, freezes=None),
                   weeks=[[str(monday - timedelta(weeks=i)), weekly[monday - timedelta(weeks=i)]] for i in range(25, -1, -1)],
                   records=[dict(label='Busiest day', value=f'{peak:,} tool calls', date=str(busiest or '—')),
                            dict(label='Longest daily streak', value=f'{best_daily} days', date=' – '.join(best_range or ['—']))],
                   achievements=badges, hours=[dict(label=label, range=span, pct=round(hours[k] / prompts * 100) if prompts else 0)
                     for k, label, span in [('early','Early','5–9 AM'),('daytime','Day','9 AM–6 PM'),('evening','Evening','6–11 PM'),('night','Late night','11 PM–5 AM')]],
                   commands=[dict(name=r['tool_name'], runs=r['runs']) for r in tools])
    return profile, projects
