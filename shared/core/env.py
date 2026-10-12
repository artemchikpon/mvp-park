"""Чтение переменных окружения с приведением типов."""

import os

_FALSE_VALUES = {"false", "0", "no"}


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    """Любое значение, кроме false/0/no (без учёта регистра и пробелов), — True."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSE_VALUES


def env_list(name: str, default: str = "") -> list[str]:
    """Список через запятую, пустые элементы отбрасываются."""
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]
