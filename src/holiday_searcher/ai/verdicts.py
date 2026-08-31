"""Cache werdyktów AI o hotelach.

Werdykt jest drogi (request do Gemini z limitem 500/dzień) i praktycznie
niezmienny — hotel nie zmienia się z dnia na dzień. Dlatego cache jest
PERMANENTNY, a nie czasowy, a jego kluczem jest to, co realnie wpływa na wynik:

    (hotel_id, prompt_version, model)

`input_hash` NIE jest częścią klucza, tylko zapisaną informacją: mówi, na jakim
materiale opinii werdykt powstał. Gdyby był kluczem, każda nowa opinia o hotelu
generowałaby nowy request — a jedna opinia więcej nie zmienia obrazu hotelu.
Kto chce odświeżyć werdykt po napływie opinii, robi to jawnie (`refresh=True`).

Zmiana PROMPT_VERSION unieważnia cache automatycznie — bo klucz jest inny.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .client import GeminiClient, GeminiError
from .opinions import HotelOpinions, WakacjeOpinions
from .pool import ROLE_BULK_VERDICT, ModelPool
from .prompts import PROMPT_VERSION, VERDICT_SCHEMA, VERDICT_SYSTEM, build_verdict_user

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_ai_verdict (
    hotel_id       TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    input_hash     TEXT NOT NULL,
    verdict_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (hotel_id, prompt_version, model)
);
"""


@dataclass
class Verdict:
    hotel_id: str
    model: str
    prompt_version: int
    data: dict[str, Any]
    created_at: str = ""
    from_cache: bool = False
    input_hash: str = ""

    # Skróty do tabelek — wszystkie mogą być None, i to jest poprawne.
    @property
    def beach(self) -> Optional[int]:
        return (self.data.get("beach") or {}).get("quality")

    @property
    def beach_notes(self) -> str:
        return (self.data.get("beach") or {}).get("notes") or ""

    @property
    def food(self) -> Optional[int]:
        return self.data.get("food")

    @property
    def cleanliness(self) -> Optional[int]:
        return self.data.get("cleanliness")

    @property
    def noise(self) -> Optional[int]:
        return self.data.get("noise")

    @property
    def family_friendly(self) -> Optional[int]:
        return self.data.get("family_friendly")

    @property
    def red_flags(self) -> list[str]:
        return [str(x) for x in (self.data.get("red_flags") or [])]

    @property
    def one_liner(self) -> str:
        return self.data.get("one_liner") or ""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def input_hash(material: str) -> str:
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


class VerdictStore:
    def __init__(self, db: sqlite3.Connection | str | Path):
        if isinstance(db, sqlite3.Connection):
            self.db = db
        else:
            path = Path(db)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)

    def get(self, hotel_id: str, model: str,
            prompt_version: int = PROMPT_VERSION) -> Verdict | None:
        row = self.db.execute(
            """SELECT * FROM hotel_ai_verdict
               WHERE hotel_id=? AND prompt_version=? AND model=?""",
            (str(hotel_id), prompt_version, model),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["verdict_json"])
        except json.JSONDecodeError:
            return None
        return Verdict(str(hotel_id), model, prompt_version, data,
                       row["created_at"], from_cache=True, input_hash=row["input_hash"])

    def any_for(self, hotel_id: str, prompt_version: int = PROMPT_VERSION) -> list[Verdict]:
        """Werdykty tego hotelu dla WSZYSTKICH modeli. Do porównywania ich
        ze sobą nie służy — modele nie są porównywalne — ale pozwala pokazać,
        co już mamy."""
        rows = self.db.execute(
            "SELECT * FROM hotel_ai_verdict WHERE hotel_id=? AND prompt_version=?",
            (str(hotel_id), prompt_version),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(Verdict(str(hotel_id), r["model"], r["prompt_version"],
                                   json.loads(r["verdict_json"]), r["created_at"],
                                   from_cache=True, input_hash=r["input_hash"]))
            except json.JSONDecodeError:
                continue
        return out

    def put(self, verdict: Verdict, provider: str = "wakacje.pl") -> None:
        self.db.execute(
            """INSERT INTO hotel_ai_verdict
               (hotel_id, provider, model, prompt_version, input_hash, verdict_json, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(hotel_id, prompt_version, model) DO UPDATE SET
                   input_hash=excluded.input_hash,
                   verdict_json=excluded.verdict_json,
                   created_at=excluded.created_at""",
            (verdict.hotel_id, provider, verdict.model, verdict.prompt_version,
             verdict.input_hash, json.dumps(verdict.data, ensure_ascii=False),
             verdict.created_at or datetime.now().isoformat(timespec="seconds")),
        )
        self.db.commit()

    def count(self, prompt_version: int = PROMPT_VERSION) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM hotel_ai_verdict WHERE prompt_version=?",
            (prompt_version,),
        ).fetchone()[0])


class VerdictService:
    """Spina cache + opinie + pulę modeli + klienta.

    Kolejność jest istotna: cache → limit → opinie → model. Najpierw najtańsze
    sprawdzenie, potem takie, które kosztują sieć albo limit.
    """

    def __init__(self, store: VerdictStore, pool: ModelPool,
                 client: GeminiClient | None = None,
                 fetcher: WakacjeOpinions | None = None,
                 role: str = ROLE_BULK_VERDICT,
                 prompt_version: int = PROMPT_VERSION):
        self.store = store
        self.pool = pool
        self.client = client or GeminiClient()
        self.fetcher = fetcher or WakacjeOpinions()
        self.role = role
        self.prompt_version = prompt_version
        self.last_error: str | None = None

    def cached(self, hotel_id: str, model: str | None = None) -> Verdict | None:
        model = model or self.pool.chain(self.role)[0]
        return self.store.get(str(hotel_id), model, self.prompt_version)

    def get_or_create(
        self,
        hotel_id: str,
        hotel_name: str,
        region: str = "",
        url: str = "",
        opinions: HotelOpinions | None = None,
        provider: str = "wakacje.pl",
        refresh: bool = False,
        strict_model: bool = True,
    ) -> Verdict | None:
        """Zwraca werdykt albo None. None NIE jest błędem — to brak danych:
        nie ma klucza, nie ma limitu, nie ma opinii albo API padło.
        Powód siedzi w `last_error`."""
        self.last_error = None
        hid = str(hotel_id)

        # 1. Cache — dla modelu dedykowanego roli (ten sam, którego użyjemy dalej).
        primary = self.pool.chain(self.role)[0]
        if not refresh:
            hit = self.store.get(hid, primary, self.prompt_version)
            if hit:
                return hit

        # 2. Klucz API. Bez niego nie ma po co pobierać opinii pod werdykt.
        if not self.client.available:
            self.last_error = "brak GEMINI_API_KEY"
            return None

        # 3. Opinie — jedyne dopuszczalne źródło wiedzy o hotelu.
        data = opinions if opinions is not None else self.fetcher.fetch(hid, url=url)
        if not data.ok:
            self.last_error = data.error or "brak opinii"
            return None

        # 4. Limit dzienny. strict_model=True trzyma jeden przebieg przy jednym
        #    modelu — werdykty z dwóch modeli w jednym rankingu byłyby
        #    porównywaniem nieporównywalnego.
        model = self.pool.acquire(self.role, strict=strict_model)
        if model is None:
            self.last_error = "wyczerpany limit dzienny"
            return None

        # 5. Wywołanie.
        user = build_verdict_user(hotel_name, region, data.opinions)
        try:
            raw = self.client.generate(model, VERDICT_SYSTEM, user, VERDICT_SCHEMA)
        except GeminiError as exc:
            self.last_error = str(exc)
            return None

        verdict = Verdict(
            hotel_id=hid, model=model, prompt_version=self.prompt_version,
            data=_normalize(raw),
            created_at=datetime.now().isoformat(timespec="seconds"),
            from_cache=False,
            input_hash=input_hash(data.fingerprint_material()),
        )
        self.store.put(verdict, provider=provider)
        return verdict


def _normalize(raw: dict) -> dict:
    """Model bywa kreatywny ze skalą mimo schematu. Ucinamy 1-5 i wymuszamy
    typy, żeby null i liczba były jedynymi możliwościami w dalszym kodzie."""
    def score(v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            return max(1, min(5, int(round(float(v)))))
        except (TypeError, ValueError):
            return None

    beach = raw.get("beach") or {}
    if not isinstance(beach, dict):
        beach = {}
    return {
        "beach": {"quality": score(beach.get("quality")),
                  "notes": (beach.get("notes") or None)},
        "food": score(raw.get("food")),
        "cleanliness": score(raw.get("cleanliness")),
        "noise": score(raw.get("noise")),
        "family_friendly": score(raw.get("family_friendly")),
        "red_flags": [str(x) for x in (raw.get("red_flags") or []) if str(x).strip()],
        "one_liner": (raw.get("one_liner") or None),
    }
