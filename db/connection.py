"""SQLAlchemy engine and connection helpers."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import config

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return a singleton SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
    return _engine


def get_connection():
    """Return a new database connection (context-manager)."""
    return get_engine().connect()
