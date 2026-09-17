"""Migration tests always use a newly-created, disposable database."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest


@pytest.fixture(scope="module")
def migrated_database():
    dsn = os.getenv("REXY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("REXY_TEST_DATABASE_URL is not configured")
    name = "rexy_contract_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        isolated = make_conninfo(dsn, dbname=name)
        try:
            with psycopg.connect(isolated, autocommit=True) as connection:
                connection.execute("""
                    do $$ begin
                      if not exists(select 1 from pg_roles where rolname='anon') then
                        create role anon nologin;
                      end if;
                      if not exists(select 1 from pg_roles where rolname='authenticated') then
                        create role authenticated nologin;
                      end if;
                    end $$;
                    create schema auth;
                    create table auth.users(id uuid primary key);
                    create function auth.uid() returns uuid language sql stable as $$
                      select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
                    $$;
                    grant usage on schema auth to authenticated;
                """)
                for migration in sorted((Path(__file__).parents[1] / "supabase/migrations").glob("*.sql")):
                    connection.execute(migration.read_text())
            yield isolated
        finally:
            # The name is generated above, never taken from caller input.
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
