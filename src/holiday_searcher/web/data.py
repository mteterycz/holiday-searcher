"""Dostęp do bazy — wszystkie zapytania dashboardu w jednym miejscu.

Baza rośnie warstwami dokładanymi przez kolejne fazy projektu i **każda
z tabel poza `offer`/`price_snapshot`/`run` może jeszcze nie istnieć** (albo
istnieć, ale być pusta). Dlatego każdy loader:

* sprawdza obecność tabeli przez `sqlite_master` (`table_exists`),
* łapie `sqlite3.OperationalError` (schemat mógł się zmienić w innej fazie),
* zwraca pustą strukturę zamiast rzucać — strona ma się wyrenderować zawsze.

Połączenie jest zawsze read-only (patrz `server.py`): monitor/`hs search`
potrafią pisać do bazy w tle.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

from ..paths import CONFIG_DIR


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def table_has_rows(conn: sqlite3.Connection, name: str) -> bool:
    if not table_exists(conn, name):
        return False
    try:
        return conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def country_label(c: str | None) -> str:
    return c if c else "Nieznany kraj"


# --------------------------------------------------------------------------
# Oferty + historia cen
# --------------------------------------------------------------------------

def _offers_with_latest(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Każda oferta + jej najnowszy i przedostatni snapshot (do liczenia zmiany)."""
    return conn.execute("""
        WITH ranked AS (
            SELECT offer_key, price, price_ppn, ts, id,
                   ROW_NUMBER() OVER (PARTITION BY offer_key ORDER BY ts DESC, id DESC) AS rn
            FROM price_snapshot
        )
        SELECT o.key, o.hotel_id, o.hotel_name, o.country, o.region, o.city, o.stars,
               o.rating, o.rating_count, o.board, o.board_raw, o.tour_operator, o.url,
               o.departure_date, o.return_date, o.nights, o.departure_place,
               latest.price AS price, latest.price_ppn AS price_ppn, latest.ts AS latest_ts,
               prev.price AS prev_price
        FROM offer o
        JOIN ranked latest ON latest.offer_key = o.key AND latest.rn = 1
        LEFT JOIN ranked prev ON prev.offer_key = o.key AND prev.rn = 2
    """).fetchall()


def max_price_by_offer(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """offer_key -> (cena maksymalna w historii, liczba snapshotów)."""
    rows = conn.execute(
        "SELECT offer_key, MAX(price) AS max_price, COUNT(*) AS n "
        "FROM price_snapshot GROUP BY offer_key"
    ).fetchall()
    return {r["offer_key"]: (r["max_price"], r["n"]) for r in rows}


def price_histories(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """offer_key -> [(ts, price), ...] chronologicznie — do sparklines."""
    rows = conn.execute(
        "SELECT offer_key, ts, price FROM price_snapshot ORDER BY offer_key, ts, id"
    ).fetchall()
    out: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        out.setdefault(r["offer_key"], []).append((r["ts"], r["price"]))
    return out


def build_offer_items(conn: sqlite3.Connection) -> list[dict]:
    """Lista ofert w postaci używanej przez wszystkie widoki."""
    rows = _offers_with_latest(conn)
    max_map = max_price_by_offer(conn)
    items: list[dict] = []
    for r in rows:
        max_price, n_snap = max_map.get(r["key"], (r["price"], 1))
        drop_amount = (max_price - r["price"]) if (max_price is not None and r["price"] is not None) else 0
        drop_pct = (drop_amount / max_price * 100.0) if max_price else 0.0
        change = None if r["prev_price"] is None else r["price"] - r["prev_price"]
        items.append({
            "key": r["key"], "hotel_id": r["hotel_id"], "hotel_name": r["hotel_name"],
            "country": r["country"], "region": r["region"], "city": r["city"],
            "stars": r["stars"], "rating": r["rating"], "rating_count": r["rating_count"],
            "board_raw": r["board_raw"] or r["board"], "tour_operator": r["tour_operator"],
            "url": r["url"], "departure_date": r["departure_date"], "return_date": r["return_date"],
            "nights": r["nights"], "departure_place": r["departure_place"],
            "price": r["price"], "price_ppn": r["price_ppn"], "change": change,
            "max_price": max_price, "n_snap": n_snap,
            "drop_amount": drop_amount, "drop_pct": drop_pct,
        })
    return items


# --------------------------------------------------------------------------
# Oceny: lokalna (offer.rating) + zewnętrzne (hotel_external_rating)
#
# Wszystkie trzy źródła są w skali 0–10: wakacje.pl oddaje ją wprost, r.pl jest
# przeliczany przez adapter (×10/6), a `hotel_external_rating.rating_0_10` ma
# skalę zapisaną w nazwie kolumny. Dzięki temu można je porównywać bezpośrednio
# i sensownie mówić o „rozjeżdżaniu się źródeł".
# --------------------------------------------------------------------------

SOURCE_LABELS = {
    "wakacje": "wakacje.pl",
    "google": "Google",
    "holidaycheck": "HolidayCheck",
}


@dataclass(frozen=True)
class RatingSource:
    key: str
    label: str
    value: float           # 0–10
    count: int | None      # liczba opinii (None = nieznana)
    url: str | None = None


@dataclass
class RatingSummary:
    """Ocena hotelu wraz z miarą zaufania do niej.

    `headline` to źródło z największą liczbą opinii — świadomie NIE liczymy
    średniej ze wszystkich: średnia z „10.0 z 1 opinii" i „8.4 z 3053 opinii"
    ukrywa dokładnie tę informację, którą ten komponent ma pokazywać.
    """
    sources: list[RatingSource] = field(default_factory=list)
    headline: RatingSource | None = None
    spread: float = 0.0            # rozstęp ocen między źródłami [pkt 0–10]
    confidence: str = "none"       # high | medium | low | thin | none

    @property
    def has_value(self) -> bool:
        return self.headline is not None


# Progi wiarygodności — liczone dla źródła wiodącego, bo to jego liczbę
# pokazujemy jako główną. 300+ opinii to statystyka, 1 opinia to anegdota.
_CONF_THRESHOLDS = ((300, "high"), (60, "medium"), (10, "low"))

CONFIDENCE_LABELS = {
    "high": "wysoka wiarygodność",
    "medium": "średnia wiarygodność",
    "low": "niska wiarygodność",
    "thin": "pojedyncze opinie",
    "none": "brak potwierdzonych ocen",
}

# Od tego rozstępu (w punktach skali 0–10) uznajemy, że źródła mówią co innego.
DISAGREE_THRESHOLD = 1.5


def _confidence_for(count: int | None) -> str:
    n = count or 0
    for threshold, name in _CONF_THRESHOLDS:
        if n >= threshold:
            return name
    return "thin"


def confidence_fraction(count: int | None) -> float:
    """0–1 do paska wiarygodności. Skala logarytmiczna: 1 opinia ≈ 10%,
    10 ≈ 33%, 100 ≈ 67%, 1000+ = 100% — liniowa dawałaby same „zera"."""
    n = max(0, count or 0)
    return min(1.0, math.log10(n + 1) / 3.0)


def load_external_ratings(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """hotel_id -> wiersze `hotel_external_rating` ze statusem `ok`.

    Tylko `status='ok'` to realna ocena; `ambiguous`/`no_match`/`no_rating`/
    `error` znaczą „nie wiemy" i nie mogą trafić na ekran jako liczba.
    """
    if not table_exists(conn, "hotel_external_rating"):
        return {}
    try:
        rows = conn.execute(
            "SELECT hotel_id, source, matched_name, rating_0_10, review_count, url, "
            "confidence, status FROM hotel_external_rating WHERE status='ok'"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r["rating_0_10"] is None:
            continue
        out.setdefault(str(r["hotel_id"]), []).append(r)
    return out


def build_rating_summary(
    local_rating: float | None,
    local_count: int | None,
    external_rows: list[sqlite3.Row] | None,
) -> RatingSummary:
    sources: list[RatingSource] = []
    if local_rating:
        sources.append(RatingSource("wakacje", SOURCE_LABELS["wakacje"],
                                    float(local_rating), local_count))
    for r in external_rows or []:
        key = str(r["source"] or "").lower()
        sources.append(RatingSource(
            key, SOURCE_LABELS.get(key, key or "źródło"),
            float(r["rating_0_10"]), r["review_count"], r["url"] or None,
        ))

    if not sources:
        return RatingSummary()

    # Wiodące = najwięcej opinii; przy remisie (albo braku liczników)
    # decyduje kolejność, w której źródła zostały dołożone.
    headline = max(sources, key=lambda s: (s.count or 0))
    values = [s.value for s in sources]
    spread = max(values) - min(values)
    return RatingSummary(
        sources=sources,
        headline=headline,
        spread=spread,
        confidence=_confidence_for(headline.count),
    )


def rating_summaries(conn: sqlite3.Connection, items: list[dict]) -> dict[str, RatingSummary]:
    """key oferty -> podsumowanie ocen (lokalna + zewnętrzne)."""
    ext = load_external_ratings(conn)
    return {
        it["key"]: build_rating_summary(
            it["rating"], it["rating_count"],
            ext.get(str(it["hotel_id"])) if it["hotel_id"] else None,
        )
        for it in items
    }


# --------------------------------------------------------------------------
# Werdykty AI
# --------------------------------------------------------------------------

# Słowa-klucze poważnych zastrzeżeń: rzeczy, które psują wyjazd nieodwracalnie
# (zdrowie, bezpieczeństwo), w odróżnieniu od „mały pokój" czy „daleko do plaży".
SEVERE_FLAG_KEYWORDS = (
    "zatru", "robak", "karaluch", "pluskw", "insekt", "owad", "mrówk",
    "grzyb", "pleśń", "plesn", "kradz", "okrad", "niebezpiecz",
    "prąd", "prad", "salmonell", "szczur", "mysz",
)

VERDICT_SCORE_LABELS = (
    ("food", "Jedzenie"),
    ("cleanliness", "Czystość"),
    ("noise", "Cisza"),
    ("family_friendly", "Dla rodzin z dziećmi"),
)


def is_severe_flag(flag: str) -> bool:
    low = str(flag).lower()
    return any(kw in low for kw in SEVERE_FLAG_KEYWORDS)


def split_flags(flags: list[str]) -> tuple[list[str], list[str]]:
    """(poważne, drobne) — poważne dostają czerwoną oprawę, drobne cichą."""
    severe = [f for f in flags if is_severe_flag(f)]
    minor = [f for f in flags if not is_severe_flag(f)]
    return severe, minor


def parse_verdict(row) -> dict | None:
    try:
        data = json.loads(row["verdict_json"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def verdict_flags(data: dict) -> list[str]:
    return [str(f).strip() for f in (data.get("red_flags") or []) if str(f).strip()]


def load_ai_lookup(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """hotel_id -> najświeższy werdykt. Pusty dict, jeśli tabeli nie ma."""
    if not table_exists(conn, "hotel_ai_verdict"):
        return {}
    try:
        rows = conn.execute("SELECT * FROM hotel_ai_verdict ORDER BY created_at ASC").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, sqlite3.Row] = {}
    for r in rows:
        out[str(r["hotel_id"])] = r  # najpóźniejszy created_at nadpisuje wcześniejsze
    return out


def load_verdict_for_hotel(conn: sqlite3.Connection, hotel_id) -> sqlite3.Row | None:
    if not hotel_id or not table_exists(conn, "hotel_ai_verdict"):
        return None
    try:
        return conn.execute(
            "SELECT * FROM hotel_ai_verdict WHERE hotel_id=? ORDER BY created_at DESC LIMIT 1",
            (hotel_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


# --------------------------------------------------------------------------
# Weryfikacja ceny (price_verification)
# --------------------------------------------------------------------------

def load_verification(conn: sqlite3.Connection, offer_key: str) -> dict | None:
    """Najświeższa weryfikacja ceny dla oferty albo None."""
    if not table_exists(conn, "price_verification"):
        return None
    try:
        row = conn.execute(
            "SELECT offer_key, checked_at, listing_price, final_price, diff_pct, details_json "
            "FROM price_verification WHERE offer_key=? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (offer_key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        details = json.loads(row["details_json"] or "{}")
    except (TypeError, ValueError):
        details = {}
    return {
        "checked_at": row["checked_at"],
        "listing_price": row["listing_price"],
        "final_price": row["final_price"],
        "diff_pct": row["diff_pct"],
        "details": details if isinstance(details, dict) else {},
    }


def verified_offer_keys(conn: sqlite3.Connection) -> set[str]:
    if not table_exists(conn, "price_verification"):
        return set()
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT offer_key FROM price_verification")}
    except sqlite3.OperationalError:
        return set()


# --------------------------------------------------------------------------
# Kalendarz cen (price_calendar)
# --------------------------------------------------------------------------

def load_calendars(conn: sqlite3.Connection) -> list[dict]:
    """Lista siatek: [{profile, checked_at, oldest, dates, nights, cells, best_key}, ...].

    Dla każdej komórki (data wylotu × liczba nocy) bierzemy jej NAJŚWIEŻSZE
    sprawdzenie — nie „ostatni przebieg" jako całość. Pojedynczy `hs kalendarz`
    bywa doliczany etapami (albo dosprawdzany dla jednej daty), więc filtr po
    jednym `checked_at` potrafiłby zredukować pełną siatkę do jednej komórki.
    Ceną za to jest siatka o mieszanym wieku — stąd `oldest` obok `checked_at`,
    żeby strona mogła to uczciwie pokazać.
    """
    if not table_exists(conn, "price_calendar"):
        return []
    try:
        rows = conn.execute(
            "SELECT profile, hotel_id, departure_date, nights, price_pp, price_ppn, checked_at "
            "FROM price_calendar ORDER BY profile, checked_at"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []

    grids: dict[str, dict[tuple[str, int], dict]] = {}
    for r in rows:
        p = r["profile"] or ""
        ts = r["checked_at"] or ""
        cell_key = (r["departure_date"], int(r["nights"]))
        cells = grids.setdefault(p, {})
        cur = cells.get(cell_key)
        # Nowszy pomiar wygrywa; przy tym samym czasie (kilka hoteli w jednym
        # przebiegu) wygrywa tańszy.
        if cur is None or ts > cur["checked_at"] or (
            ts == cur["checked_at"] and (r["price_pp"] or 0) < cur["price_pp"]
        ):
            cells[cell_key] = {
                "price_pp": r["price_pp"], "price_ppn": r["price_ppn"], "checked_at": ts,
            }

    out: list[dict] = []
    for profile, cells in grids.items():
        if not cells:
            continue
        dates = sorted({k[0] for k in cells})
        nights = sorted({k[1] for k in cells})
        stamps = sorted(c["checked_at"] for c in cells.values())
        # Minimum liczone w zł/os/noc — jedyna wielkość porównywalna między
        # kolumnami o różnej długości pobytu.
        best_key = min(cells, key=lambda k: cells[k]["price_ppn"])
        out.append({
            "profile": profile, "checked_at": stamps[-1], "oldest": stamps[0],
            "dates": dates, "nights": nights, "cells": cells, "best_key": best_key,
        })
    out.sort(key=lambda g: g["profile"])
    return out


# --------------------------------------------------------------------------
# Profile z config/profiles.yaml (poza bazą — plik może nie istnieć)
# --------------------------------------------------------------------------

def load_profiles() -> list[dict]:
    try:
        import yaml
        path = CONFIG_DIR / "profiles.yaml"
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return list(data.get("profiles") or [])
    except Exception:
        return []
