from pathlib import Path


MIGRATIONS = sorted((Path(__file__).parents[1] / "supabase" / "migrations").glob("*.sql"))
SQL = "\n".join(migration.read_text() for migration in MIGRATIONS).lower()


def test_canonical_dedup_does_not_use_payload_hash() -> None:
    assert "unique (device_id, source_file_id, source_sequence, source_item_index)" in SQL
    assert "payload_hash bytea not null unique" not in SQL


def test_owned_children_have_composite_foreign_keys() -> None:
    assert "foreign key (user_id, device_id)" in SQL
    assert "foreign key (user_id, session_id)" in SQL
    assert "foreign key (user_id, session_id, event_id)" in SQL


def test_exposed_tables_use_rls_and_no_browser_writes_are_granted() -> None:
    for table in (
        "devices", "sessions", "events", "tool_calls", "device_imports",
        "daily_rollups", "summaries",
    ):
        assert f"alter table public.{table} enable row level security" in SQL
        assert f"{table}_read_own" in SQL
    assert "grant insert" not in SQL
    assert "grant update" not in SQL
    assert "grant delete" not in SQL
