"""Sekrety: najpierw zmienna środowiskowa, potem config/.env (KEY=VALUE, # komentarze)."""
from __future__ import annotations

import os
from functools import lru_cache

from .paths import CONFIG_DIR


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    path = CONFIG_DIR / ".env"
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_secret(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _dotenv().get(name) or default
