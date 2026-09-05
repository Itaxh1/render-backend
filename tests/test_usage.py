from datetime import date
from backend.usage import aggregate_usage


def test_missing_usage_is_not_reported_as_zero():
    assert aggregate_usage([]) == ({}, {})


def test_multiple_rows_sum_instead_of_overwriting_and_thinking_is_not_added():
    row = dict(source="claude-code", local_day=date(2026, 9, 4), token_input=10,
               token_output=20, token_cache_read=30, token_cache_write=5, token_thinking=12)
    combined, sources = aggregate_usage([row, row])
    assert sources["2026-09-04"]["claude-code"].total == 130
    assert combined["2026-09-04"].out == 40
    assert combined["2026-09-04"].th == 24
