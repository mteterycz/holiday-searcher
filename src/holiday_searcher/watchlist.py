"""Watchlist konkretnych hoteli — pilnowanie NIEZALEŻNE od bieżących filtrów
profilu (np. `max_price_pp`). Użytkownik ma już finalistów i czeka, aż któryś
z nich stanieje albo padnie na historyczne minimum.

Trzy typy zdarzeń, każdy osobno liczony do anti-spamu (hotel, typ) -> N dni:

- WATCH_TARGET       — najtańszy aktualny wariant hotelu spadł <= `target_price_pp`.
- WATCH_ATH          — najtańszy aktualny wariant jest niżej niż jakikolwiek
                        wcześniej zanotowany snapshot tego hotelu (price_snapshot).
                        Wymaga wcześniejszej historii — pierwsze sprawdzenie
                        nigdy tego nie odpala (nie ma z czym porównać).
- WATCH_NEW_CHEAPEST — pojawił się NOWY wariant (inny termin/pokój/operator —
                        czyli nowy `offer.key`), którego cena jest niższa niż
                        dotychczasowy najtańszy AKTYWNY wariant tego hotelu.

Anti-spam ma WŁASNĄ tabelę (nie `notification_log` z deals.py), bo tamta jest
kluczowana `offer_key` (konkretny wariant: termin+pokój+operator), a tu chcemy
cooldown na poziomie (hotel, typ zdarzenia) — inaczej ten sam hotel z nowym
terminem ominąłby cooldown przy każdej zmianie wariantu.

Pobieranie ofert JEDNEGO hotelu: własny payload do `search.tripsSearch` z
`hotelId` ustawionym wprost (patrz `_hotel_payload`, wzorowane na `_payload`
w providers/wakacje.py, ale bez filtrów ceny/gwiazdek/wyżywienia — mają nie
obowiązywać przy pilnowaniu). Sieć i retry pożyczone z `WakacjeProvider._post`,
mapowanie odpowiedzi z `WakacjeProvider._map` — oba są re-używane, nie
duplikowane, provider pozostaje nietknięty.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .models import Offer, SearchProfile
from .providers.wakacje import DEPARTURES, SORT_CHEAPEST, WakacjeProvider

DEFAULT_COOLDOWN_DAYS = 2
DEFAULT_FETCH_LIMIT = 30

WATCH_EVENT_TYPES = ("WATCH_TARGET", "WATCH_ATH", "WATCH_NEW_CHEAPEST")

_EVENT_LABELS = {
    "WATCH_TARGET": ("\U0001F3AF", "Cena poniżej celu"),
    "WATCH_ATH": ("\U0001F3C6", "Historyczne minimum"),
    "WATCH_NEW_CHEAPEST": ("\U0001F195", "Nowy, tańszy termin"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id         TEXT NOT NULL,
    hotel_name       TEXT NOT NULL,
    provider         TEXT NOT NULL,
    profile          TEXT,
    target_price_pp  INTEGER,
    note             TEXT,
    added_at         TEXT NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_watchlist_hotel ON watchlist(hotel_id, provider, active);

CREATE TABLE IF NOT EXISTS watch_notification_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id    INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watch_notif ON watch_notification_log(watch_id, event_type, sent_at);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def _money(n: Optional[int]) -> str:
    return "-" if n is None else f"{n:,}".replace(",", " ")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- dopasowywanie hoteli (dodaj/usun) ----------

def find_matches(db: sqlite3.Connection, query: str) -> list[dict]:
    """Dopasowuje hotele z tabeli `offer` po dokładnym `hotel_id`, a gdy nic
    nie pasuje — po fragmencie nazwy (LIKE, bez rozróżniania wielkości liter;
    SQLite robi to domyślnie dla ASCII). Grupowanie po (hotel_id, provider),
    żeby wiele wierszy tego samego hotelu (różne snapshoty/regiony przez
    literówkę) nie udawało wielu różnych hoteli."""
    q = query.strip()
    rows = db.execute(
        """SELECT hotel_id, hotel_name, provider, country, region, MAX(last_seen) AS last_seen
           FROM offer WHERE hotel_id = ?
           GROUP BY hotel_id, provider""",
        (q,),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows]
    like = f"%{q}%"
    rows = db.execute(
        """SELECT hotel_id, hotel_name, provider, country, region, MAX(last_seen) AS last_seen
           FROM offer WHERE hotel_name LIKE ?
           GROUP BY hotel_id, provider""",
        (like,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- CRUD watchlisty ----------

def add_watch(db: sqlite3.Connection, hotel_id: str, hotel_name: str, provider: str,
              profile: Optional[str], target_price_pp: Optional[int],
              note: Optional[str]) -> int:
    ensure_schema(db)
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        """INSERT INTO watchlist(hotel_id, hotel_name, provider, profile,
               target_price_pp, note, added_at, active)
           VALUES (?,?,?,?,?,?,?,1)""",
        (hotel_id, hotel_name, provider, profile, target_price_pp, note, now),
    )
    db.commit()
    return int(cur.lastrowid)


def list_active(db: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_schema(db)
    return db.execute("SELECT * FROM watchlist WHERE active=1 ORDER BY added_at").fetchall()


def find_active_by_id_or_fragment(db: sqlite3.Connection, token: str) -> list[sqlite3.Row]:
    """ID watchlisty ma pierwszeństwo (jednoznaczny, to co pokazuje `lista`);
    dopiero gdy token nie jest liczbą albo nie pasuje do żadnego ID, szukamy
    po fragmencie nazwy hotelu lub hotel_id wśród aktywnych wpisów."""
    ensure_schema(db)
    token = token.strip()
    if token.isdigit():
        row = db.execute(
            "SELECT * FROM watchlist WHERE id=? AND active=1", (int(token),)
        ).fetchone()
        if row:
            return [row]
    like = f"%{token}%"
    return db.execute(
        "SELECT * FROM watchlist WHERE active=1 AND (hotel_name LIKE ? OR hotel_id LIKE ?)",
        (like, like),
    ).fetchall()


def deactivate(db: sqlite3.Connection, watch_id: int) -> None:
    ensure_schema(db)
    db.execute("UPDATE watchlist SET active=0 WHERE id=?", (watch_id,))
    db.commit()


# ---------- kontekst z bazy (offer/price_snapshot) dla listy i detekcji ----------

def hotel_location(db: sqlite3.Connection, hotel_id: str, provider: str) -> tuple[str, str]:
    row = db.execute(
        "SELECT country, region FROM offer WHERE hotel_id=? AND provider=? "
        "ORDER BY last_seen DESC LIMIT 1",
        (hotel_id, provider),
    ).fetchone()
    if row:
        return row["country"] or "", row["region"] or ""
    return "", ""


def _current_cheapest(db: sqlite3.Connection, hotel_id: str,
                       provider: str) -> Optional[tuple[str, int]]:
    """(offer_key, cena) najtańszego AKTYWNEGO (ostatni snapshot) wariantu
    tego hotelu spośród tego, co już jest w bazie."""
    row = db.execute(
        """SELECT o.key AS key, ps.price AS price FROM offer o
           JOIN price_snapshot ps ON ps.offer_key = o.key
           WHERE o.hotel_id=? AND o.provider=?
             AND ps.id = (SELECT MAX(id) FROM price_snapshot WHERE offer_key=o.key)
           ORDER BY ps.price ASC LIMIT 1""",
        (hotel_id, provider),
    ).fetchone()
    return (row["key"], row["price"]) if row else None


def current_best_price(db: sqlite3.Connection, hotel_id: str, provider: str) -> Optional[int]:
    best = _current_cheapest(db, hotel_id, provider)
    return best[1] if best else None


def _historical_min(db: sqlite3.Connection, hotel_id: str, provider: str) -> Optional[int]:
    row = db.execute(
        """SELECT MIN(ps.price) AS m FROM price_snapshot ps
           JOIN offer o ON o.key = ps.offer_key
           WHERE o.hotel_id=? AND o.provider=?""",
        (hotel_id, provider),
    ).fetchone()
    return row["m"] if row and row["m"] is not None else None


def _known_offer_keys(db: sqlite3.Connection, hotel_id: str, provider: str) -> set[str]:
    rows = db.execute(
        "SELECT key FROM offer WHERE hotel_id=? AND provider=?", (hotel_id, provider)
    ).fetchall()
    return {r["key"] for r in rows}


# ---------- pobieranie ofert jednego hotelu ----------

def _hotel_payload(hotel_id: str, profile: SearchProfile,
                    limit: int = DEFAULT_FETCH_LIMIT) -> list[dict]:
    """Payload do `search.tripsSearch` zawężony do JEDNEGO hotelu przez
    `params.hotelId`. Świadomie BEZ `maxPrice`/`minCategory`/`service` —
    watchlista ma widzieć hotel niezależnie od tego, czy akurat mieści się
    w filtrach profilu (cena chwilowo powyżej `max_price_pp` i tak ma trafić
    do detekcji). Termin/loty/pokoje bierzemy z profilu — to nie jest filtr
    okazji, tylko opis WAKACJI, które nas interesują.

    WAŻNE (zweryfikowane empirycznie): `countryId` MUSI zostać pusty, gdy
    ustawiamy `hotelId` — API po stronie wakacje.pl przy jednoczesnym
    countryId+hotelId po prostu IGNORUJE filtr hotelu i zwraca zwykłe wyniki
    kraju (widziane jako setki nietrafionych ofert innych hoteli). Sam
    `hotelId` bez `countryId` filtruje poprawnie."""
    departures = [DEPARTURES[d] for d in profile.departures if d in DEPARTURES]
    rooms = [{
        "adult": profile.adults, "kid": len(profile.children_ages),
        "ages": profile.children_ages, "inf": 0,
    }]
    return [{
        "method": "search.tripsSearch",
        "params": {
            "searchType": "wczasy", "brand": "WAK", "limit": limit,
            "priceHistory": 1, "imageSizes": ["570,428"], "flatArray": True,
            "multiSearch": True, "withHotelRate": 1, "withPromoOffer": 1,
            "recommendationVersion": "noTUI", "type": "tours",
            "firstMinuteTui": False,
            "countryId": [],
            "regionId": [], "cityId": [],
            "hotelId": [str(hotel_id)], "roundTripId": [], "cruiseId": [],
            "offersAttributes": [],
            "alternative": {"countryId": [], "regionId": [], "cityId": []},
            "query": {
                "campTypes": [], "qsVersion": 0, "qsVersionLast": 0,
                "tab": False, "candy": False, "pok": None, "flush": False,
                "tourOpAndCode": None, "obj_xCode": None, "obj_code": None,
                "obj_type": None, "catalog": None, "roomType": None,
                "test": None, "year": None, "month": None, "rangeDate": None,
                "withoutLast": 0, "category": False, "not-attribute": False,
                "pageNumber": 1,
                "departureDate": profile.date_from.isoformat(),
                "arrivalDate": profile.date_to.isoformat(),
                "departure": departures or None,
                "type": [1],
                "duration": {"min": profile.nights_min, "max": profile.nights_max},
                "minPrice": None, "maxPrice": None,
                "service": [],
                "firstminute": None, "attribute": [], "promotion": [],
                "tourId": None, "search": None,
                "minCategory": None, "maxCategory": 50,
                "sort": SORT_CHEAPEST[0], "order": SORT_CHEAPEST[1],
                "totalPrice": None, "rank": None,
                "withoutTours": [], "withoutCountry": [], "withoutTrips": [],
                "rooms": rooms,
                "offerCode": None,
            },
        },
    }]


def fetch_hotel_offers(prov, hotel_id: str, profile: SearchProfile,
                        limit: int = DEFAULT_FETCH_LIMIT) -> list[Offer]:
    """`prov` — cokolwiek z metodą `_post(payload) -> dict` zgodną z
    `WakacjeProvider` (retry/delay już tam są); w testach to zwykły dubler
    bez sieci. Mapowanie odpowiedzi pożyczone z `WakacjeProvider._map`."""
    payload = _hotel_payload(hotel_id, profile, limit=limit)
    data = prov._post(payload)
    raw = data.get("offers") or []
    out: list[Offer] = []
    for item in raw:
        offer = WakacjeProvider._map(item)
        if offer is not None:
            out.append(offer)
    return out


# ---------- zdarzenia ----------

@dataclass
class WatchEvent:
    event_type: str          # WATCH_TARGET | WATCH_ATH | WATCH_NEW_CHEAPEST
    watch_id: int
    hotel_id: str
    hotel_name: str
    region: str
    city: str
    price: int
    departure_date: str
    nights: int
    board: str
    url: str
    extra: Optional[str] = None


def _make_event(event_type: str, entry: sqlite3.Row, offer: Offer,
                 extra: Optional[str] = None) -> WatchEvent:
    return WatchEvent(
        event_type=event_type, watch_id=entry["id"], hotel_id=entry["hotel_id"],
        hotel_name=offer.hotel_name or entry["hotel_name"],
        region=offer.region, city=offer.city, price=offer.price,
        departure_date=offer.departure_date.isoformat(), nights=offer.nights,
        board=offer.board_raw or offer.board, url=offer.url, extra=extra,
    )


def check_entry(store, prov, entry: sqlite3.Row, profile: SearchProfile) -> list[WatchEvent]:
    """Pobiera aktualne oferty pilnowanego hotelu, zapisuje je do bazy
    (append-only — historia rośnie niezależnie od tego, czy coś się odpala)
    i zwraca listę zdarzeń wykrytych w TYM przebiegu (bez filtrowania przez
    anti-spam — to robi `notifiable`)."""
    db = store.db
    ensure_schema(db)
    hotel_id, provider = entry["hotel_id"], entry["provider"]

    hist_min_before = _historical_min(db, hotel_id, provider)
    known_keys_before = _known_offer_keys(db, hotel_id, provider)
    cheapest_before = _current_cheapest(db, hotel_id, provider)
    cheapest_before_price = cheapest_before[1] if cheapest_before else None

    offers = fetch_hotel_offers(prov, hotel_id, profile)
    if not offers:
        return []

    store.save(offers)  # snapshoty niezależnie od filtrów — to fundament pilnowania

    cheapest_new = min(offers, key=lambda o: o.price)
    events: list[WatchEvent] = []

    target = entry["target_price_pp"]
    if target and cheapest_new.price <= target:
        events.append(_make_event(
            "WATCH_TARGET", entry, cheapest_new,
            extra=f"Cel: {_money(target)} zł/os",
        ))

    if hist_min_before is not None and cheapest_new.price < hist_min_before:
        events.append(_make_event(
            "WATCH_ATH", entry, cheapest_new,
            extra=f"Poprzednie minimum: {_money(hist_min_before)} zł/os",
        ))

    if cheapest_before_price is not None:
        for o in sorted(offers, key=lambda o: o.price):
            if o.key not in known_keys_before and o.price < cheapest_before_price:
                events.append(_make_event(
                    "WATCH_NEW_CHEAPEST", entry, o,
                    extra=f"Poprzednio najtaniej: {_money(cheapest_before_price)} zł/os",
                ))
                break  # jeden nowy tańszy wariant wystarczy za sygnał

    return events


def format_watch_event(ev: WatchEvent) -> str:
    place = f"{ev.region} / {ev.city}" if ev.city else ev.region
    icon, label = _EVENT_LABELS.get(ev.event_type, ("\U0001F514", "Watchlist"))
    lines = [
        f"{icon} <b>{label}</b>: {_esc(ev.hotel_name)} ({_esc(place)})",
        f"{_money(ev.price)} zł/os · {ev.departure_date}, {ev.nights} nocy, {_esc(ev.board)}",
    ]
    if ev.extra:
        lines.append(_esc(ev.extra))
    lines.append(ev.url)
    return "\n".join(lines)


# ---------- anti-spam (własny, kluczowany watch_id + typ zdarzenia) ----------

def _in_cooldown(db: sqlite3.Connection, watch_id: int, event_type: str,
                  cooldown_days: int) -> bool:
    since = (datetime.now() - timedelta(days=cooldown_days)).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT 1 FROM watch_notification_log "
        "WHERE watch_id=? AND event_type=? AND sent_at>=? LIMIT 1",
        (watch_id, event_type, since),
    ).fetchone()
    return row is not None


def notifiable(db: sqlite3.Connection, events: list[WatchEvent],
               cooldown_days: int = DEFAULT_COOLDOWN_DAYS) -> list[WatchEvent]:
    ensure_schema(db)
    return [e for e in events if not _in_cooldown(db, e.watch_id, e.event_type, cooldown_days)]


def mark_sent(db: sqlite3.Connection, event: WatchEvent) -> None:
    ensure_schema(db)
    db.execute(
        "INSERT INTO watch_notification_log(watch_id, event_type, sent_at) VALUES (?,?,?)",
        (event.watch_id, event.event_type, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
