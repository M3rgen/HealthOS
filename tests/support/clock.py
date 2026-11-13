"""Test clocks: database-stamped columns and the application's wall clock.

``stamp_with_clock``: columns whose time the database stamps (``server_default=func.now()``).

SQLite's ``CURRENT_TIMESTAMP`` has whole seconds and follows the wall clock, so a test that
needed two rows stamped apart had to sleep past a second boundary. ``stamp_with_clock`` gives
the columns a Python-side default read from the test's clock instead (the server default stays
for statements that bypass SQLAlchemy). Patch before the first insert of the test's engine:
SQLAlchemy caches compiled inserts per engine.

``freeze_app_clock`` (the ``frozen_now`` fixture): the application's ``datetime.now()`` and
``date.today()`` stand still, so a test that derives "today" itself and the code under test
can never land on two sides of midnight. Tests that only place a time well before or after
the application's own now (``now - STALE_RUNNING_AFTER - 1 min``, ``now + 1 s`` as a fetch
start) keep the wall clock: their margins hold at any time of day.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime

from sqlalchemy import Column, ColumnDefault


def stamp_with_clock(monkeypatch, clock, *columns: Column) -> None:
    """Every insert that leaves ``columns`` out stamps them with ``clock.now``."""
    for column in columns:
        monkeypatch.setattr(column, "default", ColumnDefault(lambda: clock.now))


_REAL_DATETIME, _REAL_DATE = datetime, date


class _FrozenMeta(type):
    # isinstance(real_value, module.datetime) stays true while a module's name is patched.
    def __instancecheck__(cls, instance):
        return isinstance(instance, cls.real)


def freeze_app_clock(monkeypatch, at: datetime) -> datetime:
    """``datetime.now()``/``utcnow()``/``today()`` and ``date.today()`` return ``at`` in every
    loaded ``app`` module (``from datetime import datetime, date`` is the only form the app
    uses). Values compare, hash and pickle as real datetimes; only the clock stands still."""
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)

    class FrozenDatetime(_REAL_DATETIME, metaclass=_FrozenMeta):
        real = _REAL_DATETIME

        @classmethod
        def now(cls, tz=None):
            return (
                aware.astimezone(tz) if tz is not None else aware.astimezone().replace(tzinfo=None)
            )

        @classmethod
        def utcnow(cls):
            return aware.astimezone(UTC).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    class FrozenDate(_REAL_DATE, metaclass=_FrozenMeta):
        real = _REAL_DATE

        @classmethod
        def today(cls):
            return aware.astimezone().date()

    for name, module in list(sys.modules.items()):
        if module is None or not (
            name == "app" or name.startswith("app.") or name == "support.sqlite_database"
        ):
            continue
        if getattr(module, "datetime", None) is _REAL_DATETIME:
            monkeypatch.setattr(module, "datetime", FrozenDatetime)
        if getattr(module, "date", None) is _REAL_DATE:
            monkeypatch.setattr(module, "date", FrozenDate)
    return aware
