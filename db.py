import atexit
import os
from contextlib import contextmanager

# a process-wide psycopg connection pool, shared by every Postgres store. opening a
# fresh connection per query is fine for a one-off script, but the long-running
# services (the OCR endpoint and the FastAPI backend) answer many requests, and a
# reconnect + TLS handshake per query is pure overhead. the pool keeps a small set
# of connections warm and hands them out.
#
# the pool is created lazily on first use, and everything psycopg-pool is imported
# inside get_pool(), so importing a store never needs the pool or a live db — the
# sqlite stubs and the unit tests stay dependency-free.

_pool = None


def conn_kwargs() -> dict:
    # the single source of connection settings; same env vars the whole pipeline
    # uses, no credentials in code.
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ.get("DB_PORT", "4444"),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "dbname": os.environ["DB_NAME"],
    }


def get_pool():
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool

        # min_size keeps one connection warm; max_size caps how many this process may
        # hold (per Modal container / uvicorn worker), so many replicas don't exhaust
        # the server's connection limit. tune via DB_POOL_MAX.
        _pool = ConnectionPool(
            kwargs=conn_kwargs(),
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "5")),
            max_idle=60,
            timeout=30,
            open=False,
        )
        _pool.open()
        # close the pool cleanly at interpreter exit. the pool runs a background
        # worker thread; without this a short-lived script that touched the db can
        # raise "cannot join thread at interpreter shutdown" during finalization.
        # harmless for the long-running services (runs only on their shutdown).
        atexit.register(_close_pool)
    return _pool


def _close_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


@contextmanager
def connection():
    # borrow a pooled connection for one transaction: committed on a clean exit,
    # rolled back on error (psycopg-pool wraps the block in `with conn:`), then
    # returned to the pool rather than closed. same semantics the stores relied on
    # from `with psycopg.connect(...) as conn`.
    with get_pool().connection() as conn:
        yield conn
