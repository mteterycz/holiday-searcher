"""Dopasowanie tego samego hotelu u różnych dostawców.

Ten sam obiekt bywa nazywany inaczej w każdym katalogu: "Sey Beach Hotel & SPA"
u jednego, "Hotel Sey Beach Spa 4*" u drugiego. Sprowadzamy nazwy do postaci
kanonicznej i porównujemy je difflib.SequenceMatcher-em.

Dwa progi, bo pewność nie jest zerojedynkowa:

* ratio >= 0.85  -> `auto`      — traktujemy jak ten sam hotel,
* 0.60 - 0.85    -> `ambiguous` — kandydat do rozstrzygnięcia (w przyszłości przez
                                  AI). Zapisujemy go z tą etykietą i NIE decydujemy
                                  sami; kod korzystający z aliasów ma prawo takich
                                  par nie ufać.

Regiony bywają nazwane inaczej po obu stronach (r.pl mówi "Marmaris" tam, gdzie
wakacje.pl mówi "Wybrzeże Egejskie"), więc dopasowanie w obrębie samego kraju jest
dozwolone — ale z karą do pewności, przez co łatwiej ląduje w `ambiguous`.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

from .models import Offer

AUTO_THRESHOLD = 0.85       # od tego ratio uznajemy hotele za tożsame
AMBIGUOUS_THRESHOLD = 0.60  # poniżej tego w ogóle nie rozważamy pary
REGION_PENALTY = 0.10       # kara za zgodność tylko po kraju

# Słowa, które nic nie mówią o tożsamości hotelu — same w sobie nie odróżniają
# dwóch obiektów, a potrafią zaburzyć podobieństwo nazw.
NOISE_WORDS = {
    "hotel", "hotels", "hotele", "resort", "resorts", "spa", "wellness",
    "club", "klub", "apartments", "apartment", "apartamenty", "aparthotel",
    "suites", "suite", "the", "and", "by", "de", "la", "el",
    "all", "inclusive", "ai", "family",
}

_STARS = re.compile(r"(?:\d\s*\*+|\*+|★+|\bgwiazdk\w*\b)", re.IGNORECASE)
_PUNCT = re.compile(r"[^a-z0-9]+")


def strip_diacritics(text: str) -> str:
    """Usuwa polskie i tureckie znaki diakrytyczne (ł nie rozkłada się w NFKD)."""
    text = text.replace("ł", "l").replace("Ł", "L").replace("ı", "i").replace("İ", "I")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def canonical_name(name: str) -> str:
    """Nazwa hotelu sprowadzona do porównywalnej postaci.

    Gdy po wycięciu szumu nie zostaje nic (np. hotel nazywa się po prostu
    "Hotel Spa"), wracamy do wersji bez wycinania słów — inaczej wszystkie takie
    hotele stałyby się identyczne."""
    base = _PUNCT.sub(" ", strip_diacritics(_STARS.sub(" ", (name or "").lower())))
    tokens = [t for t in base.split() if t]
    meaningful = [t for t in tokens if t not in NOISE_WORDS]
    return " ".join(meaningful or tokens)


def canonical_place(name: str) -> str:
    return " ".join(_PUNCT.sub(" ", strip_diacritics((name or "").lower())).split())


def similarity(a: str, b: str) -> float:
    """Podobieństwo dwóch nazw kanonicznych (0..1)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class Hotel:
    """Hotel widziany oczami jednego dostawcy."""
    provider: str
    hotel_id: str
    name: str
    country: str
    region: str

    @property
    def canonical(self) -> str:
        return canonical_name(self.name)


@dataclass(frozen=True)
class HotelMatch:
    left: Hotel
    right: Hotel
    ratio: float          # surowe podobieństwo nazw
    confidence: float     # ratio po karze za niezgodny region
    status: str           # "auto" albo "ambiguous"
    same_region: bool

    @property
    def canonical_id(self) -> str:
        """Wspólny identyfikator pary — stabilny w czasie, niezależny od kolejności."""
        parts = sorted([
            f"{self.left.provider}:{self.left.hotel_id}",
            f"{self.right.provider}:{self.right.hotel_id}",
        ])
        seed = canonical_place(self.left.country) + "|" + "|".join(parts)
        return hashlib.sha1(seed.encode()).hexdigest()[:16]


def hotels_from_offers(offers: Iterable[Offer]) -> list[Hotel]:
    """Oferty -> unikalne hotele. Region bierzemy z pierwszej napotkanej oferty."""
    out: dict[tuple[str, str], Hotel] = {}
    for o in offers:
        key = (o.provider, o.hotel_id or o.hotel_name)
        if key not in out:
            out[key] = Hotel(o.provider, o.hotel_id or o.hotel_name,
                             o.hotel_name, o.country, o.region)
    return list(out.values())


def _status(confidence: float) -> str | None:
    if confidence >= AUTO_THRESHOLD:
        return "auto"
    if confidence >= AMBIGUOUS_THRESHOLD:
        return "ambiguous"
    return None


def match_hotels(left: Sequence[Hotel], right: Sequence[Hotel]) -> list[HotelMatch]:
    """Dopasowuje hotele między dwoma dostawcami. Zwraca pary auto i ambiguous.

    Dopasowujemy zachłannie od najlepszych par — każdy hotel może wejść tylko
    w jedną parę, żeby jedna nazwa nie „przyciągnęła" trzech obiektów naraz."""
    candidates: list[HotelMatch] = []
    for a in left:
        for b in right:
            if canonical_place(a.country) != canonical_place(b.country):
                continue  # różne kraje = z definicji różne hotele
            ratio = similarity(a.canonical, b.canonical)
            if ratio < AMBIGUOUS_THRESHOLD:
                continue
            same_region = canonical_place(a.region) == canonical_place(b.region)
            confidence = ratio if same_region else max(0.0, ratio - REGION_PENALTY)
            status = _status(confidence)
            if status is None:
                continue
            candidates.append(HotelMatch(a, b, round(ratio, 4), round(confidence, 4),
                                         status, same_region))

    candidates.sort(key=lambda m: (m.confidence, m.ratio), reverse=True)
    used_left: set[tuple[str, str]] = set()
    used_right: set[tuple[str, str]] = set()
    out: list[HotelMatch] = []
    for m in candidates:
        lk = (m.left.provider, m.left.hotel_id)
        rk = (m.right.provider, m.right.hotel_id)
        if lk in used_left or rk in used_right:
            continue
        used_left.add(lk)
        used_right.add(rk)
        out.append(m)
    return out


# ---------------------------------------------------------------- persystencja

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_alias (
    canonical_id      TEXT NOT NULL,
    provider          TEXT NOT NULL,
    provider_hotel_id TEXT NOT NULL,
    hotel_name        TEXT NOT NULL,
    canonical_name    TEXT NOT NULL,
    country           TEXT,
    region            TEXT,
    confidence        REAL NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('auto', 'ambiguous')),
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (canonical_id, provider, provider_hotel_id)
);
CREATE INDEX IF NOT EXISTS idx_alias_provider ON hotel_alias(provider, provider_hotel_id);
CREATE INDEX IF NOT EXISTS idx_alias_status   ON hotel_alias(status);
"""


class AliasStore:
    """Tabela hotel_alias. Moduł tworzy ją sam — nie zależy od storage.py."""

    def __init__(self, path: str | Path | sqlite3.Connection = "data/offers.db"):
        if isinstance(path, sqlite3.Connection):
            self.db = path
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def save_matches(self, matches: Iterable[HotelMatch]) -> int:
        """Zapisuje obie strony pary pod wspólnym canonical_id. Zwraca liczbę wierszy."""
        now = datetime.now().isoformat(timespec="seconds")
        rows = 0
        for m in matches:
            for hotel in (m.left, m.right):
                self.db.execute(
                    """INSERT INTO hotel_alias(canonical_id, provider, provider_hotel_id,
                           hotel_name, canonical_name, country, region,
                           confidence, status, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(canonical_id, provider, provider_hotel_id) DO UPDATE SET
                           hotel_name=excluded.hotel_name,
                           canonical_name=excluded.canonical_name,
                           country=excluded.country, region=excluded.region,
                           confidence=excluded.confidence, status=excluded.status,
                           updated_at=excluded.updated_at""",
                    (m.canonical_id, hotel.provider, hotel.hotel_id, hotel.name,
                     hotel.canonical, hotel.country, hotel.region,
                     m.confidence, m.status, now),
                )
                rows += 1
        self.db.commit()
        return rows

    def aliases(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.db.execute(
                "SELECT * FROM hotel_alias WHERE status=? ORDER BY canonical_id", (status,)
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM hotel_alias ORDER BY canonical_id").fetchall()

    def close(self) -> None:
        self.db.close()
