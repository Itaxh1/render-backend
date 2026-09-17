"""Finite per-process connection budget; imports cannot consume read capacity."""
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def configure_connection(connection):
    await connection.execute("set statement_timeout='15s'")
    await connection.execute("set lock_timeout='2s'")
    await connection.commit()


def pool(database_url, maximum, waiting):
    return AsyncConnectionPool(
        database_url, min_size=0, max_size=maximum, open=False,
        timeout=2, max_waiting=waiting, configure=configure_connection,
        kwargs={'row_factory':dict_row,'connect_timeout':5},
    )
