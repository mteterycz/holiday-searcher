"""Kalendarz cen — jak cena zależy od DATY WYLOTU i DŁUGOŚCI POBYTU.

Motywacja: profil ma sztywne okno terminów, a rynek nie. Przesunięcie wylotu
o 2–3 dni albo skrócenie pobytu o jedną noc potrafi zbić cenę o kilkanaście
procent — tego nie widać w liście TOP-15, bo tam każdy wiersz to inny hotel.
Tu odwracamy perspektywę: jedna oś to data, druga to liczba nocy, a w komórce
siedzi najtańsza oferta, jaka w tym terminie w ogóle istnieje.

Zasady, których trzymamy się w tym module:

* provider jest tylko konsumowany (`search`), nigdy modyfikowany; warianty
  profilu powstają przez `dataclasses.replace` — SearchProfile jest frozen;
* jedno zapytanie na jedną datę wylotu (okno zawężone do tej daty), bo
  sortowanie po cenie przy szerokim oknie zwróciłoby najtańsze oferty
  z CAŁEGO okna i większość komórek zostałaby pusta;
* porównania robimy w zł za osobę za noc (`Offer.price_ppn`) — to jedyna
  wielkość porównywalna między kolumnami o różnej liczbie nocy;
* baza `data/offers.db` bywa zapisywana przez inne procesy, więc trzymamy
  krótkie połączenia i append-only zapis (jak `price_snapshot`).
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import httpx

from .models import Offer, SearchProfile
from .providers.wakacje import (
    DEPARTURES,
    ENDPOINT,
    SERVICE_CODES,
    SORT_CHEAPEST,
    UA,
    BASE,
    WakacjeProvider,
)

DEFAULT_SPREAD = 5
DEFAULT_LIMIT = 60
# Bezpiecznik na koszt: jeden dzień = co najmniej jedno zapytanie na kierunek.
DEFAULT_MAX_DATES = 24
MIN_DELAY = 1.5
# Ile procent nad minimum wciąż uznajemy za "praktycznie tak samo tanio".
NEAR_MIN_PCT = 0.05

# hotel_id dla wiersza dotyczącego całego profilu (a nie jednego hotelu).
# Pusty string zamiast NULL, żeby klucz główny tabeli działał bez pułapek
# porównywania NULL-i w SQLite.
ALL_HOTELS = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_calendar (
    profile        TEXT NOT NULL,
    hotel_id       TEXT NOT NULL DEFAULT '',
    departure_date TEXT NOT NULL,
    nights         INTEGER NOT NULL,
    price_pp       INTEGER NOT NULL,
    price_ppn      REAL NOT NULL,
    checked_at     TEXT NOT NULL,
    PRIMARY KEY (profile, hotel_id, departure_date, nights, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_price_calendar_run
    ON price_calendar(profile, hotel_id, checked_at);
"""


@dataclass(frozen=True)
class Cell:
    """Najtańsza oferta dla jednej kombinacji (data wylotu × liczba nocy)."""
    departure_date: date
    nights: int
    price_pp: int          # najtańsza cena całkowita za osobę [zł]
    price_ppn: float       # ta sama oferta w zł/os/noc — do porównań między kolumnami
    hotel_name: str = ""
    hotel_id: str = ""
    url: str = ""

    @property
    def key(self) -> tuple[date, int]:
        return (self.departure_date, self.nights)


# --------------------------------------------------------------------------
# okno dat i warianty profilu
# --------------------------------------------------------------------------

def departure_dates(profile: SearchProfile, spread: int = DEFAULT_SPREAD,
                    max_dates: int | None = DEFAULT_MAX_DATES,
                    today: date | None = None) -> list[date]:
    """Daty wylotu do sprawdzenia: okno profilu poszerzone o `spread` dni
    w obie strony.

    Dwa zawężenia, oba świadome:
    * daty z przeszłości wypadają — API i tak nic dla nich nie zwróci,
      a każde zapytanie kosztuje;
    * jeśli okno jest dłuższe niż `max_dates`, przycinamy je SYMETRYCZNIE
      wokół środka, żeby budżet zapytań nie rósł z rozmiarem profilu, a to,
      co zostaje, było nadal wyśrodkowane na terminie użytkownika.
    """
    if spread < 0:
        raise ValueError("spread nie może być ujemny")
    if profile.date_to < profile.date_from:
        raise ValueError("date_to jest wcześniejsze niż date_from")

    start = profile.date_from - timedelta(days=spread)
    end = profile.date_to + timedelta(days=spread)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    if today is not None:
        days = [d for d in days if d >= today]

    if max_dates and len(days) > max_dates:
        offset = (len(days) - max_dates) // 2
        days = days[offset:offset + max_dates]
    return days


def window_profile(profile: SearchProfile, day: date) -> SearchProfile:
    """Wariant profilu zawężony do JEDNEJ daty wylotu.

    `date_to` to górna granica powrotu, nie wylotu — musi zostawić miejsce na
    najdłuższy dopuszczalny pobyt (+1 dzień luzu, bo część touroperatorów
    liczy dobę powrotu inaczej)."""
    return dataclasses.replace(
        profile,
        date_from=day,
        date_to=day + timedelta(days=profile.nights_max + 1),
    )


def in_profile_window(profile: SearchProfile, day: date) -> bool:
    """Czy data mieści się w oryginalnym oknie profilu (a nie w rozszerzeniu)."""
    return profile.date_from <= day <= profile.date_to


# --------------------------------------------------------------------------
# agregacja
# --------------------------------------------------------------------------

def aggregate(offers: Iterable[Offer],
              dates: Sequence[date] | None = None,
              nights_range: tuple[int, int] | None = None) -> dict[tuple[date, int], Cell]:
    """Redukuje listę ofert do siatki: dla każdej pary (data wylotu, liczba nocy)
    zostaje NAJTAŃSZA oferta.

    W obrębie jednej komórki liczba nocy jest stała, więc „najtańsza cena za
    osobę" i „najtańsza cena za osobę za noc" wskazują tę samą ofertę — dlatego
    wystarczy jedno minimum, a obie wielkości zapisujemy obok siebie.
    """
    allowed = set(dates) if dates is not None else None
    grid: dict[tuple[date, int], Cell] = {}

    for o in offers:
        if not o.nights or o.price <= 0:
            continue                     # oferta bez ceny/długości jest bezużyteczna
        if allowed is not None and o.departure_date not in allowed:
            continue
        if nights_range and not (nights_range[0] <= o.nights <= nights_range[1]):
            continue
        key = (o.departure_date, o.nights)
        current = grid.get(key)
        if current is None or o.price < current.price_pp:
            grid[key] = Cell(
                departure_date=o.departure_date,
                nights=o.nights,
                price_pp=int(o.price),
                price_ppn=round(o.price_ppn, 2),
                hotel_name=o.hotel_name,
                hotel_id=o.hotel_id,
                url=o.url,
            )
    return grid


def best_cell(grid: dict[tuple[date, int], Cell]) -> Cell | None:
    """Globalne minimum liczone w zł/os/noc — jedyna wielkość porównywalna
    między kolumnami. Gdyby brać cenę całkowitą, zawsze wygrywałby najkrótszy
    pobyt i siatka nie niosłaby żadnej informacji."""
    if not grid:
        return None
    return min(grid.values(), key=lambda c: (c.price_ppn, c.price_pp, c.departure_date))


def near_minimum_keys(grid: dict[tuple[date, int], Cell], pct: float = NEAR_MIN_PCT
                      ) -> set[tuple[date, int]]:
    """Komórki w granicach `pct` od minimum (bez samego minimum) — „równie
    dobre" terminy, o których warto wiedzieć, że są elastyczne."""
    best = best_cell(grid)
    if best is None or best.price_ppn <= 0:
        return set()
    threshold = best.price_ppn * (1 + pct)
    return {k for k, c in grid.items()
            if c.price_ppn <= threshold and k != best.key}


def column_minimums(grid: dict[tuple[date, int], Cell]) -> dict[int, Cell]:
    """Najtańszy termin w każdej kolumnie (dla danej liczby nocy).
    To porównanie jabłek z jabłkami — pokazuje czysty efekt przesunięcia daty."""
    out: dict[int, Cell] = {}
    for cell in grid.values():
        cur = out.get(cell.nights)
        if cur is None or cell.price_pp < cur.price_pp:
            out[cell.nights] = cell
    return out


def profile_window_best(grid: dict[tuple[date, int], Cell], profile: SearchProfile,
                        nights: int | None = None) -> Cell | None:
    """Najtańsza komórka mieszcząca się w ORYGINALNYM oknie profilu — punkt
    odniesienia dla zdania „o ile taniej niż w terminie z profilu"."""
    candidates = [c for c in grid.values() if in_profile_window(profile, c.departure_date)]
    if nights is not None:
        candidates = [c for c in candidates if c.nights == nights]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.price_pp, c.departure_date))


def money(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def summarize(grid: dict[tuple[date, int], Cell], profile: SearchProfile) -> str:
    """Jedno zdanie podsumowania pod siatką — to ono odpowiada na pytanie
    „czy przesunięcie terminu się opłaca"."""
    best = best_cell(grid)
    if best is None:
        return "Brak danych — żadna kombinacja daty i długości pobytu nie zwróciła oferty."

    head = (f"Najtaniej {best.departure_date:%d.%m} na {best.nights} nocy — "
            f"{money(best.price_pp)} zł/os ({money(best.price_ppn)} zł/os/noc)")
    if best.hotel_name:
        head += f", {best.hotel_name}"

    # Odniesienie: ta sama liczba nocy, ale wylot z okna zapisanego w profilu.
    # Porównywanie różnych długości pobytu byłoby nieuczciwe.
    baseline = profile_window_best(grid, profile, nights=best.nights)
    if baseline is None:
        return head + ". Brak oferty na tyle nocy w oknie z profilu — nie ma do czego porównać."
    if baseline.key == best.key:
        alt = _second_best_in_window(grid, profile, best)
        tail = ". Najtańszy termin mieści się w oknie profilu"
        if alt is not None and alt.price_pp > best.price_pp:
            diff = alt.price_pp - best.price_pp
            tail += (f" — ale nawet w jego środku rozrzut sięga {money(diff)} zł/os "
                     f"({diff / alt.price_pp * 100:.0f}%)")
        return head + tail + "."

    diff = baseline.price_pp - best.price_pp
    if diff <= 0:
        # Minimum wypadło poza oknem profilu, ale wewnątrz okna da się kupić
        # to samo za tę samą cenę — nie ma czego reklamować jako oszczędności.
        return (head + f". Tyle samo kosztuje wylot {baseline.departure_date:%d.%m} "
                "z okna profilu — samo przesunięcie poza okno nic tu nie daje.")
    pct = diff / baseline.price_pp * 100 if baseline.price_pp else 0.0
    return (head + f", o {money(diff)} zł/os taniej niż w terminie z profilu "
            f"(tam najtaniej {baseline.departure_date:%d.%m} za {money(baseline.price_pp)} zł/os, "
            f"różnica {pct:.0f}%).")


def _second_best_in_window(grid: dict[tuple[date, int], Cell], profile: SearchProfile,
                           best: Cell) -> Cell | None:
    """Najdroższa komórka o tej samej długości pobytu w oknie profilu — służy
    do pokazania rozrzutu, gdy minimum i tak wypadło w oknie użytkownika."""
    same = [c for c in grid.values()
            if c.nights == best.nights and in_profile_window(profile, c.departure_date)]
    if len(same) < 2:
        return None
    return max(same, key=lambda c: c.price_pp)


def spread_report(grid: dict[tuple[date, int], Cell], max_rows: int = 5) -> list[tuple[int, Cell, Cell, float]]:
    """Dla każdej liczby nocy: (noce, najtańszy termin, najdroższy termin, %).
    Odpowiada wprost na „ile daje samo przesunięcie daty przy tej samej
    długości pobytu"."""
    by_nights: dict[int, list[Cell]] = {}
    for cell in grid.values():
        by_nights.setdefault(cell.nights, []).append(cell)

    rows = []
    for nights, cells in by_nights.items():
        if len(cells) < 2:
            continue
        cheap = min(cells, key=lambda c: c.price_pp)
        pricey = max(cells, key=lambda c: c.price_pp)
        if pricey.price_pp <= 0:
            continue
        pct = (pricey.price_pp - cheap.price_pp) / pricey.price_pp * 100
        rows.append((nights, cheap, pricey, pct))
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows[:max_rows]


# --------------------------------------------------------------------------
# pobieranie: cały profil
# --------------------------------------------------------------------------

def collect_profile(provider, profile: SearchProfile, dates: Sequence[date],
                    limit: int = DEFAULT_LIMIT, delay: float = MIN_DELAY,
                    progress: Callable[[date, int], None] | None = None) -> list[Offer]:
    """Po jednym przebiegu wyszukiwania na każdą datę wylotu.

    `provider.search` sam rozdziela limit na kierunki i sam robi przerwy między
    stronami; my dokładamy przerwę MIĘDZY datami, bo to osobne serie zapytań.
    """
    out: list[Offer] = []
    for i, day in enumerate(dates):
        offers = provider.search(window_profile(profile, day), limit=limit)
        out.extend(offers)
        if progress:
            progress(day, len(offers))
        if i < len(dates) - 1:
            time.sleep(max(MIN_DELAY, delay))
    return out


# --------------------------------------------------------------------------
# pobieranie: jeden hotel (własne wywołanie API)
# --------------------------------------------------------------------------

def hotel_payload(profile: SearchProfile, day: date, hotel_id: str,
                  page: int = 1, limit: int = 30) -> list[dict[str, Any]]:
    """Payload `search.tripsSearch` zawężony do jednego hotelu.

    Wzorowany na `WakacjeProvider._payload`, ale świadomie inny w trzech
    miejscach: `hotelId` niesie identyfikator, a `countryId`/`regionId` są puste
    (hotel jednoznacznie wskazuje kraj, a filtr kraju mógłby go tylko wyciąć,
    gdyby profil był wielokierunkowy). Nie ruszamy `service`, bo użytkownik
    wybrał hotel — nie chcemy mu ukrywać wariantu ze śniadaniem, jeśli profil
    domyślnie żąda All Inclusive.
    """
    departures = [DEPARTURES[d] for d in profile.departures if d in DEPARTURES]
    rooms = [{
        "adult": profile.adults,
        "kid": len(profile.children_ages),
        "ages": profile.children_ages,
        "inf": 0,
    }]
    return [{
        "method": "search.tripsSearch",
        "params": {
            "searchType": "wczasy", "brand": "WAK", "limit": limit,
            "priceHistory": 1, "imageSizes": ["570,428"], "flatArray": True,
            "multiSearch": True, "withHotelRate": 1, "withPromoOffer": 1,
            "recommendationVersion": "noTUI", "type": "tours",
            "firstMinuteTui": False,
            "countryId": [], "regionId": [], "cityId": [],
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
                "pageNumber": page,
                "departureDate": day.isoformat(),
                "arrivalDate": (day + timedelta(days=profile.nights_max + 1)).isoformat(),
                "departure": departures or None,
                "type": [1],
                "duration": {"min": profile.nights_min, "max": profile.nights_max},
                "minPrice": None, "maxPrice": None,
                "service": [SERVICE_CODES[b] for b in profile.boards if b in SERVICE_CODES],
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


class HotelCalendarClient:
    """Cienki klient do trybu `--hotel`.

    Osobny od providera z premedytacją: provider nie zna pojęcia „jeden hotel",
    a dokładanie tam parametru zmieniałoby moduł, z którego korzystają inne
    komendy. Mapowanie odpowiedzi reużywamy z providera (`_map`) — to jedyne
    miejsce w projekcie, które wie, jak wygląda oferta wakacje.pl.
    """

    def __init__(self, delay: float = MIN_DELAY, timeout: float = 45.0):
        self.delay = max(MIN_DELAY, delay)
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": f"{BASE}/",
                "Origin": BASE,
            },
        )

    def close(self) -> None:
        self.client.close()

    def _post(self, payload: list[dict[str, Any]], attempts: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                r = self.client.post(ENDPOINT, content=json.dumps(payload))
                r.raise_for_status()
                body = r.json()
                if not body.get("success"):
                    raise RuntimeError(f"wakacje.pl: {body.get('error') or body.get('msg')}")
                return body.get("data") or {}
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last = exc
                if attempt == attempts:
                    break
                time.sleep(self.delay * (2 ** attempt))
        raise RuntimeError(f"wakacje.pl (kalendarz hotelu): nieudane po {attempts} próbach: {last}") from last

    def offers_for_day(self, profile: SearchProfile, day: date, hotel_id: str,
                       limit: int = 30, page_size: int = 30) -> Iterator[Offer]:
        page = 1
        yielded = 0
        while True:
            data = self._post(hotel_payload(profile, day, hotel_id, page, page_size))
            raw = data.get("offers") or []
            if not raw:
                return
            for item in raw:
                offer = WakacjeProvider._map(item)
                if offer is None:
                    continue
                # Filtr lotniska po stronie klienta — parametr API grupuje lotniska
                # jednego miasta (Chopin i Modlin dzielą id 278).
                if profile.departures and offer.departure_code not in profile.departures:
                    continue
                yield offer
                yielded += 1
                if yielded >= limit:
                    return
            if len(raw) < page_size:
                return
            page += 1
            time.sleep(self.delay)


def collect_hotel(client: "HotelCalendarClient", profile: SearchProfile,
                  dates: Sequence[date], hotel_id: str, limit: int = DEFAULT_LIMIT,
                  delay: float = MIN_DELAY,
                  progress: Callable[[date, int], None] | None = None) -> list[Offer]:
    # `--limit` to budżet na CAŁY kalendarz; rozdzielamy go na dni, ale nie
    # schodzimy poniżej 10 ofert dziennie — mniej nie pokryłoby kolumn z nocami.
    per_day = max(10, limit // max(1, len(dates)))
    out: list[Offer] = []
    for i, day in enumerate(dates):
        offers = list(client.offers_for_day(profile, day, hotel_id, limit=per_day))
        out.extend(offers)
        if progress:
            progress(day, len(offers))
        if i < len(dates) - 1:
            time.sleep(max(MIN_DELAY, delay))
    return out


# --------------------------------------------------------------------------
# zapis / odczyt
# --------------------------------------------------------------------------

@contextmanager
def _connect(db_path: str | Path):
    """Krótkie połączenie — baza jest współdzielona z innymi procesami
    (monitor z launchd, dashboard), więc nie trzymamy jej otwartej."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30.0)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def ensure_schema(db: sqlite3.Connection) -> None:
    """Idempotentne — wszystkie CREATE mają IF NOT EXISTS."""
    db.executescript(SCHEMA)
    db.commit()


def save_calendar(db_path: str | Path, profile_name: str,
                  grid: dict[tuple[date, int], Cell], hotel_id: str = ALL_HOTELS,
                  checked_at: str | None = None) -> str:
    """Zapisuje siatkę jako jeden przebieg. Append-only: `checked_at` jest
    częścią klucza głównego, więc kolejne przebiegi budują historię zamiast
    kasować poprzednią (dokładnie jak `price_snapshot`). Zwraca `checked_at`."""
    stamp = checked_at or datetime.now().isoformat(timespec="seconds")
    rows = [
        (profile_name, hotel_id or ALL_HOTELS, c.departure_date.isoformat(),
         c.nights, c.price_pp, c.price_ppn, stamp)
        for c in grid.values()
    ]
    with _connect(db_path) as db:
        ensure_schema(db)
        db.executemany(
            """INSERT OR REPLACE INTO price_calendar
               (profile, hotel_id, departure_date, nights, price_pp, price_ppn, checked_at)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        db.commit()
    return stamp


def load_calendar(db_path: str | Path, profile_name: str,
                  hotel_id: str = ALL_HOTELS, checked_at: str | None = None
                  ) -> tuple[str | None, dict[tuple[date, int], Cell]]:
    """Odczytuje przebieg (domyślnie najświeższy) jako siatkę.
    Zwraca (checked_at, grid); (None, {}) gdy nic nie zapisano.

    Nazwy hoteli nie ma w tabeli — kolumny wynikają wprost ze specyfikacji —
    więc odtworzone komórki mają puste pola opisowe. Do rysowania siatki
    i liczenia różnic to wystarcza."""
    with _connect(db_path) as db:
        ensure_schema(db)
        if checked_at is None:
            row = db.execute(
                "SELECT MAX(checked_at) AS ts FROM price_calendar WHERE profile=? AND hotel_id=?",
                (profile_name, hotel_id or ALL_HOTELS),
            ).fetchone()
            checked_at = row["ts"] if row else None
            if not checked_at:
                return None, {}
        rows = db.execute(
            """SELECT departure_date, nights, price_pp, price_ppn FROM price_calendar
               WHERE profile=? AND hotel_id=? AND checked_at=?""",
            (profile_name, hotel_id or ALL_HOTELS, checked_at),
        ).fetchall()

    grid: dict[tuple[date, int], Cell] = {}
    for r in rows:
        day = date.fromisoformat(r["departure_date"])
        cell = Cell(departure_date=day, nights=int(r["nights"]),
                    price_pp=int(r["price_pp"]), price_ppn=float(r["price_ppn"]))
        grid[cell.key] = cell
    return checked_at, grid
