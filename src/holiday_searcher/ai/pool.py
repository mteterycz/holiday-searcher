"""Pula modeli Gemini z twardym pilnowaniem limitów.

KAŻDY model ma OSOBNY limit (RPM/RPD) — to nie jest jedna wspólna pula.
Stąd kubełek per model, a nie per konto.

Licznik dzienny (RPD) musi przeżyć restart procesu, więc siedzi w SQLite
(`ai_usage`). Licznik minutowy (RPM) nie musi — okno minutowe i tak wygasa
szybciej niż typowa przerwa między uruchomieniami, więc trzymamy je w pamięci
i egzekwujemy prostym sleepem.

Rola opisuje, DO CZEGO model jest użyty; z roli wynika łańcuch failoveru.
Nazwy modeli mogą wymagać korekty do realnych identyfikatorów API — dlatego
są stałymi w jednym miejscu, a nie wklejone w kod wywołań.
"""
from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    model    TEXT NOT NULL,
    day      TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (model, day)
);
"""

# Role:
#   bulk-misc    — normalizacja/dedup (tanie, masowe, mało istotne pomyłki)
#   bulk-verdict — werdykty jakościowe hoteli (masowe, ale liczy się treść)
#   deep         — vibe match (jedno wywołanie, duży kontekst, ma być dobre)
#   experimental — eksperymenty, NIE używać w produkcji
ROLE_BULK_MISC = "bulk-misc"
ROLE_BULK_VERDICT = "bulk-verdict"
ROLE_DEEP = "deep"
ROLE_EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    rpm: int          # requests per minute
    rpd: int          # requests per day
    tpm: int          # tokens per minute (informacyjnie — nie liczymy tokenów lokalnie)
    role: str


# Limity z konta użytkownika. Modele lite mają hojne RPD (500), modele "flash"
# tylko 20 dziennie — dlatego 3.5-flash jest zarezerwowany dla jednego,
# zbiorczego wywołania vibe, a nie dla pętli po hotelach.
MODELS: dict[str, ModelSpec] = {
    "gemini-3.5-flash-lite": ModelSpec("gemini-3.5-flash-lite", 15, 500, 250_000, ROLE_BULK_MISC),
    "gemini-3.1-flash-lite": ModelSpec("gemini-3.1-flash-lite", 15, 500, 250_000, ROLE_BULK_VERDICT),
    "gemini-3.5-flash":      ModelSpec("gemini-3.5-flash", 5, 20, 250_000, ROLE_DEEP),
    "gemini-3.6-flash":      ModelSpec("gemini-3.6-flash", 5, 20, 250_000, ROLE_DEEP),
    "gemini-3.7-flash":      ModelSpec("gemini-3.7-flash", 5, 20, 250_000, ROLE_EXPERIMENTAL),
}

# Kolejność = kolejność failoveru. Pierwszy model to dedykacja, reszta to zapas.
# Uwaga: failover ZMIENIA model, a werdykty z różnych modeli nie są porównywalne —
# dlatego w jednym przebiegu rankingowym warto trzymać się jednego modelu
# (patrz `ModelPool.acquire(..., strict=True)`).
ROLE_CHAINS: dict[str, list[str]] = {
    ROLE_BULK_MISC:    ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    ROLE_BULK_VERDICT: ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    ROLE_DEEP:         ["gemini-3.5-flash", "gemini-3.6-flash"],
    ROLE_EXPERIMENTAL: ["gemini-3.7-flash"],
}


class QuotaExhausted(RuntimeError):
    """Wyczerpany limit dzienny na wszystkich modelach danej roli."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


class ModelPool:
    """Kubełki per model z licznikiem dziennym w SQLite.

    `sleeper`/`monotonic`/`today` są wstrzykiwane, żeby testy mogły egzekwować
    limity bez czekania na realny zegar.
    """

    def __init__(
        self,
        db: sqlite3.Connection | str | Path,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        today: Callable[[], str] | None = None,
    ):
        if isinstance(db, sqlite3.Connection):
            self.db = db
        else:
            path = Path(db)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)
        self._sleep = sleeper
        self._monotonic = monotonic
        self._today = today or (lambda: date.today().isoformat())
        self._windows: dict[str, deque[float]] = {}

    # ---------- limity ----------

    def used_today(self, model: str) -> int:
        row = self.db.execute(
            "SELECT requests FROM ai_usage WHERE model=? AND day=?", (model, self._today())
        ).fetchone()
        return int(row["requests"]) if row else 0

    def remaining(self, model: str) -> int:
        spec = MODELS.get(model)
        if spec is None:
            return 0
        return max(0, spec.rpd - self.used_today(model))

    def chain(self, role: str) -> list[str]:
        if role not in ROLE_CHAINS:
            raise ValueError(f"Nieznana rola: {role!r}. Znane: {', '.join(ROLE_CHAINS)}")
        return list(ROLE_CHAINS[role])

    # ---------- rezerwacja ----------

    def acquire(self, role: str, strict: bool = False) -> str | None:
        """Rezerwuje JEDNO wywołanie dla roli i zwraca nazwę modelu.

        Zwraca None, gdy limit dzienny jest wyczerpany na całym łańcuchu roli —
        wywołujący ma wtedy zdegradować się po cichu, nie wywalić.
        `strict=True` ogranicza się do modelu dedykowanego (bez failoveru),
        żeby jeden przebieg rankingowy nie zmieszał werdyktów z dwóch modeli.

        UWAGA: acquire LICZY request. Wywołuj bezpośrednio przed wysłaniem
        zapytania — nie „na zapas".
        """
        candidates = self.chain(role)[:1] if strict else self.chain(role)
        for model in candidates:
            if self.remaining(model) <= 0:
                continue
            self._wait_for_rpm(model)
            self._record(model)
            return model
        return None

    def acquire_or_raise(self, role: str, strict: bool = False) -> str:
        model = self.acquire(role, strict=strict)
        if model is None:
            raise QuotaExhausted(
                f"Wyczerpany limit dzienny dla roli {role!r} "
                f"({', '.join(self.chain(role))})"
            )
        return model

    def _wait_for_rpm(self, model: str) -> None:
        spec = MODELS[model]
        window = self._windows.setdefault(model, deque())
        now = self._monotonic()
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= spec.rpm:
            wait = 60.0 - (now - window[0]) + 0.05
            if wait > 0:
                self._sleep(wait)
            now = self._monotonic()
            while window and now - window[0] >= 60.0:
                window.popleft()
        window.append(now)

    def _record(self, model: str) -> None:
        self.db.execute(
            """INSERT INTO ai_usage(model, day, requests) VALUES (?,?,1)
               ON CONFLICT(model, day) DO UPDATE SET requests = requests + 1""",
            (model, self._today()),
        )
        self.db.commit()

    # ---------- raport ----------

    def usage(self, days: int = 7) -> list[dict]:
        rows = self.db.execute(
            "SELECT model, day, requests FROM ai_usage ORDER BY day DESC, model"
        ).fetchall()
        seen_days: list[str] = []
        out: list[dict] = []
        for r in rows:
            if r["day"] not in seen_days:
                if len(seen_days) >= days:
                    break
                seen_days.append(r["day"])
            spec = MODELS.get(r["model"])
            out.append({
                "model": r["model"],
                "day": r["day"],
                "requests": int(r["requests"]),
                "rpd": spec.rpd if spec else None,
                "role": spec.role if spec else "?",
            })
        return out


def models_for_role(role: str) -> Iterable[ModelSpec]:
    for name in ROLE_CHAINS.get(role, []):
        if name in MODELS:
            yield MODELS[name]
