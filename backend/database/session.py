"""Engine / oturum yönetimi. Varsayılan SQLite; DATABASE_URL ile PostgreSQL'e geçilebilir."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.config import get_settings

from .base import Base

_lock = threading.Lock()
_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        path = url.split("///", 1)[-1] if "///" in url else ""
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if ":memory:" in url or url.rstrip("/") == "sqlite://":
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def configure_database(url: str | None = None, *, create_tables: bool = True) -> Engine:
    """Engine'i (yeniden) yapılandırır. url verilmezse settings.database_url."""
    global _engine, _factory
    import backend.models  # noqa: F401  (tabloları Base.metadata'ya kaydeder)

    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = _make_engine(url or get_settings().database_url)
        _factory = sessionmaker(_engine, expire_on_commit=False)
        if create_tables:
            Base.metadata.create_all(_engine)
        return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure_database()
    assert _engine is not None
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _factory is not None
    session = _factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
