"""Время в БД хранится как naive UTC (историческое решение схемы).

``datetime.utcnow()`` объявлен устаревшим в Python 3.12, поэтому все места,
где нужен "сейчас в UTC без tzinfo", используют :func:`utcnow`.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
