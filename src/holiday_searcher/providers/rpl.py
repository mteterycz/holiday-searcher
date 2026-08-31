"""Adapter r.pl (Rainbow Tours) — drugie źródło ofert.

Rekonesans fazy 4 (szczegóły: docs/faza4-drugie-zrodlo.md). Serwis to Nuxt 3;
oferty ładowane po stronie klienta, bez Cloudflare i bez logowania.

Pobranie jednej strony wyników wymaga DWÓCH zapytań — API jest rozbite na:

1. `POST https://r.pl/api/wyszukiwarka/v5.0/wyszukaj`
   zwraca `{Count, CzyCenaZaOsobe, Wynik: [{Id, Cena, LiczbaDni, TerminWyjazdu, …}]}`
   — same identyfikatory i ceny, BEZ nazw hoteli.
2. `POST https://r.pl/api/bloczki/v5.0/pobierz-bloczki`
   dostaje w `Parametry` surowe elementy `Wynik` i dokłada opis hotelu
   (nazwa, gwiazdki, region, wyżywienie, ocena, lotniska, URL oferty).

CENA: domyślnie API zwraca cenę ZA CAŁĄ GRUPĘ. Dopiero atrybut
`Cena: ["avg", "*-*"]` przełącza je na cenę za osobę — potwierdza to flaga
`CzyCenaZaOsobe` w odpowiedzi. Adapter zawsze wysyła "avg" i dodatkowo
asekuracyjnie dzieli przez liczbę osób, gdyby flaga wróciła jako False.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterator

import httpx

from ..models import Offer, SearchProfile
from .base import Provider

BASE = "https://r.pl"
SEARCH_ENDPOINT = f"{BASE}/api/wyszukiwarka/v5.0/wyszukaj"
BLOCKS_ENDPOINT = f"{BASE}/api/bloczki/v5.0/pobierz-bloczki"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# r.pl adresuje kierunki slugami "kontynent:panstwo[:region]", nie liczbowymi id.
COUNTRIES = {
    "turcja": "europa:turcja",
    "grecja": "europa:grecja",
    "hiszpania": "europa:hiszpania",
    "bulgaria": "europa:bulgaria",
    "albania": "europa:albania",
    "cypr": "europa:cypr",
    "chorwacja": "europa:chorwacja",
    "czarnogora": "europa:czarnogora",
    "wlochy": "europa:wlochy",
    "portugalia": "europa:portugalia",
    "egipt": "afryka:egipt",
    "tunezja": "afryka:tunezja",
    "maroko": "afryka:maroko",
    "emiraty": "bliski-wschod:zjednoczone-emiraty-arabskie",
}

# Regiony Turcji — nazwy własne r.pl, INNE niż u wakacje.pl (stąd dedup po kraju).
REGIONS = {
    "riwiera turecka": "europa:turcja:riwiera-turecka",
    "riwiera egejska": "europa:turcja:riwiera-egejska",
    "marmaris": "europa:turcja:marmaris",
    "dalaman": "europa:turcja:dalaman",
    "mersin": "europa:turcja:mersin",
    "kapadocja": "europa:turcja:kapadocja",
    "stambul": "europa:turcja:stambul",
}

# Wyżywienie: r.pl ma tylko pięć koszyków i NIE rozróżnia UAI / AI Plus / AI Soft.
# Do zapytania wysyłamy koszyk, w mapowaniu wyniku wracamy do kodów BOARD_TIERS.
BOARD_TO_FILTER = {
    "AI": "all-inclusive", "UAI": "all-inclusive",
    "AI_PLUS": "all-inclusive", "AI_SOFT": "all-inclusive",
    "FB": "3-posilki", "HB": "2-posilki", "BB": "sniadania",
}

# Nazwy wyżywienia z bloczka -> kody kanoniczne. Kolejność ma znaczenie:
# najbardziej szczegółowe warianty najpierw.
BOARD_FROM_NAME = (
    ("ultra all inclusive", "UAI"),
    ("all inclusive plus", "AI_PLUS"),
    ("all inclusive soft", "AI_SOFT"),
    ("soft all inclusive", "AI_SOFT"),
    ("all inclusive", "AI"),
    ("3 posilki", "FB"),
    ("3 posiłki", "FB"),
    ("2 posilki", "HB"),
    ("2 posiłki", "HB"),
    ("sniadania", "BB"),
    ("śniadania", "BB"),
)

SORT_CHEAPEST = "cena-asc"
SORT_POPULAR = "rekomendowane-desc"
SORT_RATING = "ocena-desc"

# Data urodzenia zamiast wieku — API liczy wiek na dzień wyjazdu.
ADULT_BIRTHDATE = "1990-01-01"

# r.pl ocenia hotele w skali szkolnej 1–6 (filtr "od 5.0" = wartość 10 z zakresu 0–12),
# a model kanoniczny i scoring.py operują na skali 0–10 (tak liczy wakacje.pl).
RATING_SCALE = 10.0 / 6.0


class RplProvider(Provider):
    name = "r.pl"

    def __init__(self, delay: float = 1.5, timeout: float = 45.0):
        self.delay = delay
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

    # ---------- budowa zapytania ----------

    @staticmethod
    def _locations(profile: SearchProfile) -> list[str]:
        if profile.regions:
            out = []
            for r in profile.regions:
                r = str(r)
                # slug podany wprost albo nazwa regionu do przetłumaczenia
                out.append(r if ":" in r else REGIONS.get(r.lower().strip(), r))
            return out
        country = COUNTRIES.get(profile.country.lower())
        if not country:
            raise ValueError(f"Nieznany kraj: {profile.country!r} — dopisz do COUNTRIES")
        return [country]

    @staticmethod
    def _birthdates(profile: SearchProfile) -> list[str]:
        today = date.today()
        kids = [
            (today.replace(year=today.year - max(int(age), 0))).isoformat()
            for age in profile.children_ages
        ]
        return [ADULT_BIRTHDATE] * profile.adults + kids

    def _payload(self, profile: SearchProfile, page: int, limit: int,
                 sort: str) -> dict[str, Any]:
        boards = sorted({BOARD_TO_FILTER[b] for b in profile.boards if b in BOARD_TO_FILTER})

        attrs: dict[str, Any] = {
            "Lokalizacje_HoteloProdukt": self._locations(profile),
            "Miasta": list(profile.departures),          # kody IATA, np. ["WAW"]
            "TypTransportu": ["AIR", "DREAMLINER"],
            # [wyjazd od, powrót do] — nie [od, do] wyjazdu; zbyt wąskie okno = 0 wyników
            "TerminWyjazdu": [profile.date_from.isoformat(), profile.date_to.isoformat()],
            # filtr długości jest w DNIACH (doba przylotu + noclegi)
            "DlugoscPobytu": [f"{profile.nights_min + 1}-{profile.nights_max + 1}"],
            # "avg" = ceny za osobę; bez tego API zwraca cenę za całą grupę
            "Cena": ["avg", f"*-{profile.max_price_pp}" if profile.max_price_pp else "*-*"],
        }
        if boards:
            attrs["Wyzywienia"] = boards
        if profile.stars_min:
            # 4* = "8", 5* = "10" — wartość filtra to gwiazdki × 2
            attrs["StandardHotelu"] = [str(s * 2) for s in range(profile.stars_min, 6)]

        return {
            "Sortowanie": sort,
            "CzyWeekendowka": False,
            "PowrotNaInneLotnisko": False,
            "Strona": page,
            "Limit": limit,
            "Atrybuty": attrs,
            "AtrybutyWyklucz": {},
            "DatyUrodzenia": self._birthdates(profile),
            "LiczbaPokoi": 1,
        }

    def _post(self, url: str, payload: Any) -> Any:
        r = self.client.post(url, content=json.dumps(payload))
        r.raise_for_status()
        return r.json()

    # ---------- pobieranie ----------

    def count(self, profile: SearchProfile) -> int:
        data = self._post(SEARCH_ENDPOINT, self._payload(profile, 1, 1, SORT_CHEAPEST))
        return int(data.get("Count") or 0)

    def search(self, profile: SearchProfile, limit: int | None = None) -> list[Offer]:
        return list(self.iter_offers(profile, limit=limit))

    def search_reference(self, profile: SearchProfile, limit: int = 300) -> list[Offer]:
        """Próbka do median koszyków — sortowana po rekomendacjach, nie po cenie."""
        return list(self.iter_offers(profile, limit=limit, sort=SORT_POPULAR))

    def iter_offers(self, profile: SearchProfile, limit: int | None = None,
                    page_size: int = 30, sort: str = SORT_CHEAPEST) -> Iterator[Offer]:
        persons = profile.adults + len(profile.children_ages)
        seen: set[str] = set()
        page = 1
        yielded = 0
        while True:
            data = self._post(SEARCH_ENDPOINT, self._payload(profile, page, page_size, sort))
            results = data.get("Wynik") or []
            if not results:
                break
            time.sleep(self.delay)
            blocks = self._fetch_blocks(results, profile, bool(data.get("CzyCenaZaOsobe")))
            by_date = {r.get("Id"): r.get("TerminWyjazdu") for r in results}

            for block in blocks:
                offer = self._map(block, by_date.get(block.get("Klucz")), persons, profile)
                if offer is None or offer.key in seen:
                    continue
                # r.pl filtruje długość w dniach — noce docinamy po swojemu
                if not (profile.nights_min <= offer.nights <= profile.nights_max):
                    continue
                seen.add(offer.key)
                yield offer
                yielded += 1
                if limit and yielded >= limit:
                    return
            if len(results) < page_size:
                break
            page += 1
            time.sleep(self.delay)

    def _fetch_blocks(self, results: list[dict], profile: SearchProfile,
                      price_per_person: bool) -> list[dict]:
        payload = {
            "Parametry": results,
            "CzyCenaZaOsobe": price_per_person,
            "CzyZmienicZdjecia": False,
            "DatyUrodzenia": self._birthdates(profile),
            "LiczbaPokoi": 1,
            "Route": "/",
        }
        blocks = self._post(BLOCKS_ENDPOINT, payload)
        return blocks if isinstance(blocks, list) else []

    # ---------- mapowanie ----------

    @classmethod
    def _map(cls, block: dict[str, Any], termin: Any, persons: int,
             profile: SearchProfile) -> Offer | None:
        try:
            info = block.get("BazoweInformacje") or {}
            nights = int(info.get("LiczbaNocy") or 0)
            dep = _parse_date(termin) or _parse_date(block.get("TerminWyjazdu"))
            if not dep or not nights:
                return None

            cena = block.get("Cena") or {}
            price = _as_float(cena.get("Cena")) or 0.0
            price_old = _as_float(cena.get("CenaPrzedPromocja")) or 0.0
            # Pas bezpieczeństwa: gdyby atrybut "avg" przestał działać, flaga to wyłapie.
            if not cena.get("CzyCenaZaOsobe") and persons:
                price /= persons
                price_old /= persons

            boards = block.get("Wyzywienia") or []
            board_raw = (boards[0].get("Nazwa") if boards else "") or ""
            place, code = _departure(block.get("Przystanki") or [], profile.departures)
            ocena = block.get("Ocena") or {}
            rating_count = _as_int(ocena.get("IloscOcen"))
            regions = info.get("Regiony") or []
            countries = info.get("Panstwa") or []
            url = info.get("OfertaUrl") or info.get("OfertaUrlNoParams") or ""

            return Offer(
                provider="r.pl",
                hotel_name=(info.get("NazwaHoteluWWW") or info.get("OfertaNazwa") or "").strip(),
                hotel_id=str(info.get("HotelId") or ""),
                # r.pl to sklep własny Rainbow Tours — organizator jest zawsze ten sam
                tour_operator="Rainbow",
                country=(countries[0] if countries else "").strip(),
                region=(regions[0] if regions else "").strip(),
                city=_city(info.get("Lokalizacje"), regions),
                stars=float(info.get("GwiazdkiHotelu") or 0),
                departure_date=dep,
                return_date=dep + timedelta(days=nights),
                nights=nights,
                board=board_code(board_raw),
                board_raw=board_raw.strip(),
                departure_place=place,
                departure_code=code,
                room_type="",       # bloczek nie podaje typu pokoju
                price=int(round(price)),
                price_old=int(round(price_old)),
                # brak opinii => 0 ocen; nie udajemy, że hotel ma ocenę
                rating=_rating_10(ocena.get("Ocena")) if rating_count else None,
                rating_count=rating_count or None,
                url=f"{BASE}{url}" if url.startswith("/") else (url or BASE),
                raw_id=str(block.get("Klucz") or block.get("UnikalnyKluczOferty") or ""),
            )
        except (TypeError, ValueError):
            return None


def board_code(name: str) -> str:
    """Nazwa wyżywienia z r.pl -> kod z models.BOARD_TIERS."""
    n = " ".join((name or "").lower().split())
    for needle, code in BOARD_FROM_NAME:
        if needle in n:
            return code
    return "OTHER"


def _rating_10(v: Any) -> float | None:
    """Skala 1–6 z r.pl przeliczona na wspólną skalę 0–10."""
    raw = _as_float(v)
    return round(raw * RATING_SCALE, 2) if raw else None


def _departure(stops: list[dict], wanted: list[str]) -> tuple[str, str]:
    """Jedna oferta r.pl bywa dostępna z kilkunastu lotnisk w tej samej cenie.
    Gdy profil nie wskazuje lotniska, nie zmyślamy jednego — oznaczamy zbiór."""
    if not stops:
        return "", ""
    if wanted:
        for s in stops:
            if (s.get("Iata") or "") in wanted:
                return (s.get("Nazwa") or "").strip(), (s.get("Iata") or "").strip()
    if len(stops) == 1:
        return (stops[0].get("Nazwa") or "").strip(), (stops[0].get("Iata") or "").strip()
    return f"dowolne ({len(stops)} lotnisk)", "*"


def _city(lokalizacje: Any, regions: list) -> str:
    """"Turcja: Marmaris" -> "Marmaris"."""
    if isinstance(lokalizacje, str) and ":" in lokalizacje:
        return lokalizacje.split(":", 1)[1].strip()
    return (regions[0] if regions else "").strip()


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
