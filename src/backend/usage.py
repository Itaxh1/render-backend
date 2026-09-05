"""Normalize provider usage before returning dashboard counts.

Claude input excludes cache reads/writes. Codex input includes cached input.
Reasoning is a subset of output, never an additional contribution to total.
Rows must already be deltas (not cumulative session snapshots).
"""
from .models import AgentTokens, DashboardTokens


def aggregate_usage(rows):
    combined = {}
    by_source = {}
    for row in rows:
        raw_input = int(row["token_input"] or 0)
        cached = int(row["token_cache_read"] or 0)
        written = int(row["token_cache_write"] or 0)
        output = int(row["token_output"] or 0)
        fresh = max(0, raw_input - cached - written) if row["source"] == "codex" else raw_input
        total = raw_input + output if row["source"] == "codex" else raw_input + cached + written + output
        values = dict(input=fresh, out=output, cr=cached, cw=written,
                      th=int(row["token_thinking"] or 0))
        day = row["local_day"].isoformat()
        agent = by_source.setdefault(day, {}).setdefault(row["source"],
            {**dict.fromkeys(values, 0), "total": 0})
        agent["total"] += total
        all_agents = combined.setdefault(day, dict.fromkeys(values, 0))
        for field, value in values.items():
            agent[field] += value
            all_agents[field] += value
    return (
        {day: DashboardTokens(**values) for day, values in combined.items()},
        {day: {source: AgentTokens(**values) for source, values in sources.items()}
         for day, sources in by_source.items()},
    )
