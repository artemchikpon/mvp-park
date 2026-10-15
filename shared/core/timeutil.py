"""Время в БД хранится как naive UTC (историческое решение схемы).

``datetime.utcnow()`` объявлен устаревшим в Python 3.12, поэтому все места,
где нужен "сейчас в UTC без tzinfo", используют :func:`utcnow`.
"""

from datetime import datetime, timezone

# Таймзона парка по умолчанию (park_settings.timezone): в ней считаются
# "сегодня", почасовые/дневные графики и время ежедневного сброса persons.
# Хранение в БД при этом остаётся в UTC. Изменить: PUT /settings/.
DEFAULT_PARK_TIMEZONE = "Asia/Tashkent"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
