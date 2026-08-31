"""Adapter wakacje.pl.

Endpoint odkryty w fazie 0: POST https://www.wakacje.pl/v2/api/offers
Baza URL składana w bundlu jako ["/v2", "/api"].join("") — stąd /v2/api.
Payload to tablica z jednym obiektem {method: "search.tripsSearch", params: {...}}.
Odpowiedź: {success, data: {count, offers: [...]}}.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Iterator

import httpx

from ..models import Destination, Offer, SearchProfile
from .base import Provider

BASE = "https://www.wakacje.pl"
ENDPOINT = f"{BASE}/v2/api/offers"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# dictService z /v2/_data/dictionary.js
SERVICE_CODES = {
    "AI": 1, "HB": 2, "BB": 3, "OWN": 4, "PROGRAM": 5,
    "FB": 6, "SNACK": 7, "DINNER": 8, "UAI": 9, "AI_SOFT": 10, "AI_PLUS": 44,
}
SERVICE_TO_BOARD = {v: k for k, v in SERVICE_CODES.items()}

SORT_CHEAPEST = (1, 0)
SORT_POPULAR = (13, 1)
SORT_RATING = (11, 1)

# dictCountriesWithRegionsAndCities z /v2/_data/dictionary.js
COUNTRIES = {
    "turcja": "16", "grecja": "29", "wlochy": "31", "włochy": "31",
    "hiszpania": "33", "malta": "99", "cypr": "110", "portugalia": "74",
    "egipt": "37", "tunezja": "65", "albania": "436", "czarnogora": "283",
    "czarnogóra": "283",
}

DEPARTURES = {
    "BZG": 269, "GDN": 2880, "KTW": 2622, "KRK": 2696, "LUZ": 272, "LCJ": 2654,
    "SZY": 2891, "POZ": 2632, "RZE": 1909, "SZZ": 2904, "WAW": 278,
    "RDO": 11688, "WRO": 256, "IEG": 306, "BER": 3301, "DRS": 10305, "OSR": 4837,
    # Modlin dzieli parametr API z Chopinem (oba to "Warszawa" = 278);
    # rozróżnia je dopiero departurePlaceCode w odpowiedzi.
    "WMI": 278,
}


# dictPlaneDepartures: nazwa miasta wylotu -> slug segmentu URL (odmiana gramatyczna,
# więc słownik jawny zamiast transliteracji)
DEPARTURE_URL_SLUGS = {
    "Warszawa": "z-warszawy", "Warszawa - Chopin": "z-warszawy",
    "Warszawa - Radom": "z-warszawy-radom",
    "Berlin": "z-berlina", "Bydgoszcz": "z-bydgoszczy", "Drezno": "z-drezna",
    "Gdańsk": "z-gdanska", "Katowice": "z-katowic", "Kraków": "z-krakowa",
    "Lublin": "z-lublina", "Łódź": "z-lodzi", "Olsztyn": "z-olsztyna",
    "Ostrawa": "z-ostrawy", "Poznań": "z-poznania", "Rzeszów": "z-rzeszowa",
    "Szczecin": "ze-szczecina", "Wrocław": "z-wroclawia",
    "Zielona Góra": "z-zielonej-gory",
}


class WakacjeProvider(Provider):
    name = "wakacje.pl"

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

    def _payload(self, profile: SearchProfile, page: int, limit: int,
                 sort: tuple[int, int], leg: Destination | None = None) -> list[dict[str, Any]]:
        leg = leg or profile.legs()[0]
        country_id = COUNTRIES.get(leg.country.lower())
        if not country_id:
            raise ValueError(f"Nieznany kraj: {leg.country!r} — dopisz do COUNTRIES")

        services = [SERVICE_CODES[b] for b in leg.boards if b in SERVICE_CODES]
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
                "countryId": [country_id], "regionId": list(leg.regions), "cityId": [],
                "hotelId": [], "roundTripId": [], "cruiseId": [],
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
                    "departureDate": profile.date_from.isoformat(),
                    "arrivalDate": profile.date_to.isoformat(),
                    "departure": departures or None,
                    "type": [1],  # 1 = samolot
                    "duration": {"min": profile.nights_min, "max": profile.nights_max},
                    "minPrice": None,
                    "maxPrice": leg.max_price_pp or profile.max_price_pp,
                    "service": services,
                    "firstminute": None, "attribute": [], "promotion": [],
                    "tourId": None, "search": None,
                    "minCategory": profile.stars_min * 10 or None,
                    "maxCategory": 50,
                    "sort": sort[0], "order": sort[1],
                    "totalPrice": None, "rank": None,
                    "withoutTours": [], "withoutCountry": [], "withoutTrips": [],
                    "rooms": rooms,
                    "offerCode": None,
                },
            },
        }]

    def _post(self, payload: list[dict[str, Any]], attempts: int = 4) -> dict[str, Any]:
        """Retry z narastającym odstępem. Powód: laptop zasypia w trakcie przebiegu
        i zrywa połączenie (RemoteProtocolError), a monitor chodzi co godzinę —
        pojedynczy błąd sieci nie może kładć całego wyszukiwania."""
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
                time.sleep(self.delay * (2 ** attempt))   # 3s, 6s, 12s
        raise RuntimeError(f"wakacje.pl: nieudane po {attempts} próbach: {last}") from last

    # ---------- pobieranie ----------

    def count(self, profile: SearchProfile, leg: Destination | None = None) -> int:
        if leg is None:
            return sum(self.count(profile, l) for l in profile.legs())
        data = self._post(self._payload(profile, page=1, limit=1,
                                        sort=SORT_CHEAPEST, leg=leg))
        return int(data.get("count") or 0)

    def counts_by_leg(self, profile: SearchProfile) -> dict[str, int]:
        out = {}
        for leg in profile.legs():
            out[leg.name] = self.count(profile, leg)
            time.sleep(self.delay)
        return out

    def search(self, profile: SearchProfile, limit: int | None = None) -> list[Offer]:
        """Limit jest rozdzielany NA KIERUNEK, nie globalnie — inaczej najtańszy
        kraj zjadłby całą pulę i reszta kierunków w ogóle by się nie pokazała."""
        legs = profile.legs()
        if len(legs) == 1:
            return list(self.iter_offers(profile, limit=limit, leg=legs[0]))
        per_leg = max(1, (limit or 120) // len(legs))
        out: list[Offer] = []
        for leg in legs:
            out.extend(self.iter_offers(profile, limit=per_leg, leg=leg))
            time.sleep(self.delay)
        return out

    def search_reference(self, profile: SearchProfile, limit: int = 300) -> list[Offer]:
        """Próbka do statystyk rynku — sortowana po popularności, nie po cenie,
        żeby mediana koszyka odzwierciedlała rynek, a nie tani ogon.
        Też rozdzielana per kierunek: każdy kraj potrzebuje własnego koszyka."""
        legs = profile.legs()
        per_leg = max(1, limit // max(1, len(legs)))
        out: list[Offer] = []
        for leg in legs:
            out.extend(self.iter_offers(profile, limit=per_leg,
                                        sort=SORT_POPULAR, leg=leg))
            time.sleep(self.delay)
        return out

    def iter_offers(self, profile: SearchProfile, limit: int | None = None,
                    page_size: int = 30, sort=SORT_CHEAPEST,
                    leg: Destination | None = None) -> Iterator[Offer]:
        """Stronicuje po wynikach. Sortowanie po cenie rosnąco sprawia, że przy
        limicie dostajemy najtańsze oferty, a nie losowe."""
        seen: set[str] = set()
        page = 1
        yielded = 0
        while True:
            data = self._post(self._payload(profile, page, page_size, sort, leg))
            raw = data.get("offers") or []
            if not raw:
                break
            for item in raw:
                offer = self._map(item)
                if offer is None or offer.key in seen:
                    continue
                # Filtr ocen po stronie klienta (serwerowy `rank` wywala upstream).
                # Brak oceny traktujemy jak niespełnienie progu — brak danych
                # nie może być przepustką.
                if profile.rating_min and (offer.rating or 0) < profile.rating_min:
                    seen.add(offer.key)
                    continue
                # Filtr lotniska po stronie klienta: parametr API grupuje lotniska
                # jednego miasta (278 = Chopin ORAZ Modlin), więc dopiero kod
                # z odpowiedzi pozwala je rozdzielić.
                if profile.departures and offer.departure_code not in profile.departures:
                    seen.add(offer.key)
                    continue
                seen.add(offer.key)
                yield offer
                yielded += 1
                if limit and yielded >= limit:
                    return
            if len(raw) < page_size:
                break
            page += 1
            time.sleep(self.delay)

    # ---------- mapowanie ----------

    @staticmethod
    def _map(item: dict[str, Any]) -> Offer | None:
        try:
            place = item.get("place") or {}
            dep = _parse_date(item.get("departureDate"))
            ret = _parse_date(item.get("returnDate"))
            nights = int(item.get("durationNights") or item.get("duration") or 0)
            if not dep or not nights:
                return None
            service = item.get("service")
            url_name = item.get("urlName") or ""
            hotel_id = str(item.get("hotelId") or item.get("id") or "")
            country_slug = (place.get("country") or {}).get("slug") or ""
            raw_id = str(item.get("offerId") or item.get("id") or "")
            dep_place = (item.get("departurePlace") or "").strip()
            # Deep-link do KONKRETNEJ oferty: /oferty/{slug}-{offerId}.html z pierwszym
            # parametrem query jako listą segmentów słownikowych (od-/do-/z-miasta/
            # samolotem) — gramatyka potwierdzona w bundlu parsera URL wakacje.pl.
            if url_name and raw_id:
                segs = []
                if item.get("departureDate"):
                    segs.append(f"od-{str(item['departureDate'])[:10]}")
                if item.get("returnDate"):
                    segs.append(f"do-{str(item['returnDate'])[:10]}")
                dep_slug = DEPARTURE_URL_SLUGS.get(dep_place)
                if dep_slug:
                    segs.append(dep_slug)
                segs.append("samolotem")
                url = f"{BASE}/oferty/{url_name}-{raw_id}.html?" + ",".join(segs)
            elif url_name and country_slug and hotel_id:
                url = f"{BASE}/hotele/{country_slug}/{url_name}-{hotel_id}.html"
            else:
                url = BASE
            return Offer(
                provider="wakacje.pl",
                hotel_name=(item.get("name") or "").strip(),
                hotel_id=hotel_id,
                tour_operator=(item.get("tourOperatorName") or "?").strip(),
                country=(place.get("country") or {}).get("name") or "",
                region=(place.get("region") or {}).get("name") or "",
                city=(place.get("city") or {}).get("name") or "",
                stars=float(item.get("category") or 0) ,
                departure_date=dep,
                return_date=ret or dep,
                nights=nights,
                board=SERVICE_TO_BOARD.get(service, "OTHER"),
                board_raw=(item.get("serviceDesc") or "").strip(),
                departure_place=(item.get("departurePlace") or "").strip(),
                departure_code=(item.get("departurePlaceCode") or "").strip(),
                room_type=(item.get("roomType") or "").strip(),
                price=int(item.get("price") or 0),
                price_old=int(item.get("priceOld") or 0),
                # API zwraca 0.0 dla hoteli bez opinii — to brak danych, nie ocena zero
                rating=_as_float(item.get("ratingValue")) or None,
                rating_count=_as_int(item.get("ratingRecommends")) or None,
                url=url,
                raw_id=str(item.get("offerId") or item.get("id") or ""),
            )
        except (TypeError, ValueError):
            return None


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
