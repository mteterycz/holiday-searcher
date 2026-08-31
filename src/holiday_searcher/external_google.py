"""Trzecie źródło prawdy o hotelu — Google Places API (New).

PO CO TO JEST
-------------
HolidayCheck (`external_ratings.py`) jest dobrym drugim źródłem, ale ma wąskie
gardło: to niemiecki katalog biur podróży, więc hotelu spoza jego oferty
w nim po prostu nie ma, a nazwy bywają nierozstrzygalne (trzy hotele „Karbel"
w Ölüdeniz). Na 25 hotelach dawało to 14 trafień, 10 `ambiguous` i 1 bez opinii.

Google zna praktycznie każdy obiekt noclegowy na świecie i podaje przy nim
`rating` (1-5) oraz `userRatingCount` — często setki opinii tam, gdzie
wakacje.pl ma jedną. To jest źródło, które ma pokrycie radykalnie poprawić.

Google NIE zastępuje HolidayCheck. Trzy źródła, które się zgadzają, są warte
więcej niż dwa; dwa, które się kłócą, są warte tyle co jedno — patrz
`external_ratings.reliability_multi`.

CZEGO SIĘ SPODZIEWAĆ PO GOOGLE (i czego nie)
--------------------------------------------
Google ocenia **szeroką publicznością**: ocenia gość restauracji hotelowej,
przechodzień z plaży, kierowca, który zaparkował. wakacje.pl i HolidayCheck
pytają wyłącznie ludzi, którzy w hotelu NOCOWALI. Dlatego Google systematycznie
ocenia wyżej i inaczej niż oba pozostałe źródła — a to znaczy, że surowa
różnica ocen mierzy kulturę oceniania, nie jakość hotelu. Stąd kalibracja
per źródło (`external_ratings.calibrate`) liczona z bieżącej próbki, nie
zaszyta w kodzie.

Opinii Google zwraca **maksymalnie 5** (`MAX_REVIEWS`) — to twardy limit API,
nie błąd i nie brak uprawnień. Do werdyktów AI używamy i tak tekstów
z wakacje.pl; od Google bierzemy przede wszystkim `rating` + `userRatingCount`.

API — dokładnie to, co stoi w dokumentacji
------------------------------------------
1. WYSZUKANIE. `POST https://places.googleapis.com/v1/places:searchText`
   Nagłówki: `X-Goog-Api-Key`, `X-Goog-FieldMask` (**field mask jest
   OBOWIĄZKOWY** — bez niego metoda zwraca błąd, nie ma listy domyślnej).
   Ciało: `{"textQuery": "...", "maxResultCount": 5, "languageCode": "pl"}`.
   Odpowiedź: `{"places": [{id, displayName{text}, rating, userRatingCount,
   formattedAddress, location{latitude,longitude}, types[]}], "nextPageToken"}`.
2. SZCZEGÓŁY/OPINIE (opcjonalne). `GET https://places.googleapis.com/v1/places/{id}`
   z field maskiem zawierającym `reviews`.

Field mask decyduje o cenniku: `id` to SKU Essentials (IDs Only),
`displayName/formattedAddress/location/types` to Pro, `rating/userRatingCount`
to Enterprise, a `reviews` to Enterprise + Atmosphere. Dlatego wyszukiwanie
prosi o komplet potrzebny do dopasowania i oceny (Enterprise), a o `reviews`
pytamy tylko wtedy, gdy ktoś ich wprost zażąda.

DOPASOWANIE HOTELU
------------------
Nauki poprzednika z HolidayCheck przenoszą się 1:1 i są tu WZMOCNIONE
(patrz `pick_place`): kraj jako warunek konieczny (weryfikowany przez
`formattedAddress`), zdejmowanie nawiasów z nazw, reguła rywala oraz — nowość
możliwa dopiero dzięki Google — filtr `types`, który odsiewa restauracje,
biura podróży i plaże o nazwie hotelu.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Iterable, Optional

import httpx

from . import config
from .external_ratings import (
    MATCH_THRESHOLD, STRICT_NAME_THRESHOLD, ST_AMBIGUOUS, ST_ERROR, ST_NO_KEY,
    ST_NO_MATCH, ST_NO_RATING, ST_OK, ExternalRating, _strip_accents,
    is_compatible, name_similarity, normalize_name, normalize_to_10, search_query,
)

SOURCE = "google"
API_KEY_NAME = "GOOGLE_PLACES_API_KEY"

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
# Publiczny link do obiektu — `id` wystarcza, sluga nie trzeba znać.
MAPS_URL = "https://www.google.com/maps/place/?q=place_id:{place_id}"

# Minimum potrzebne do dopasowania (`displayName`, `formattedAddress`, `types`)
# i do oceny (`rating`, `userRatingCount`). `location` bierzemy, bo pozwala
# zrobić `locationBias` przy kolejnym hotelu w tej samej miejscowości.
# Bez spacji — dokumentacja wprost tego zabrania.
SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.rating,places.userRatingCount,"
    "places.formattedAddress,places.location,places.types"
)
# `reviews` przenosi żądanie do najdroższego SKU (Enterprise + Atmosphere),
# więc jest w osobnej masce, używanej tylko na wyraźne życzenie.
DETAILS_FIELD_MASK = "id,displayName,rating,userRatingCount,reviews,googleMapsUri"

# Twardy limit API: Place Details oddaje najwyżej 5 opinii i nie ma
# paginacji ani sortowania. To ograniczenie Google, nie nasze.
MAX_REVIEWS = 5

# Google ocenia w skali 1-5; wszystko w tym projekcie żyje w skali 0-10.
GOOGLE_BEST_RATING = 5

# O ile podbijamy w rankingu kandydata z właściwej miejscowości (patrz
# `pick_place` — premia wpływa TYLKO na kolejność, nigdy na pewność).
PLACE_BONUS = 0.30

# Typy z „Table A / Lodging" w Places API (New). Obiekt, który nie ma
# ŻADNEGO z nich, nie jest hotelem — to restauracja, plaża albo biuro
# podróży o tej samej nazwie.
LODGING_TYPES = frozenset({
    "hotel", "motel", "resort_hotel", "extended_stay_hotel", "inn",
    "guest_house", "bed_and_breakfast", "hostel", "cottage", "farmstay",
    "campground", "camping_cabin", "private_guest_room", "japanese_inn",
    "budget_japanese_inn", "mobile_home_park", "rv_park", "lodging",
    # Apartamenty wakacyjne bywają skatalogowane jako mieszkaniowe.
    "apartment_complex", "apartment_building",
})

# `formattedAddress` przychodzi w języku z `languageCode`, ale Google
# lokalizuje nazwy krajów niekonsekwentnie (raz „Grecja", raz „Greece",
# raz forma miejscowa). Przyjmujemy każdą z nich — kraj to warunek konieczny,
# więc fałszywe ODRZUCENIE jest tu równie kosztowne co fałszywe przyjęcie.
COUNTRY_ALIASES = {
    "grecja": ("grecja", "greece", "ellada", "hellas"),
    "turcja": ("turcja", "turkiye", "turkey", "turkei"),
    "włochy": ("wlochy", "italy", "italia", "italien"),
    "wlochy": ("wlochy", "italy", "italia", "italien"),
    "hiszpania": ("hiszpania", "spain", "espana", "spanien"),
    "malta": ("malta",),
    "cypr": ("cypr", "cyprus", "kibris", "kypros"),
    "portugalia": ("portugalia", "portugal"),
    "egipt": ("egipt", "egypt", "misr"),
    "chorwacja": ("chorwacja", "croatia", "hrvatska"),
    "albania": ("albania", "shqiperia", "shqiperi"),
    "bułgaria": ("bulgaria", "balgariya"),
    "bulgaria": ("bulgaria", "balgariya"),
    "tunezja": ("tunezja", "tunisia", "tunisie", "tunis"),
    "maroko": ("maroko", "morocco", "maroc"),
    "czarnogóra": ("czarnogora", "montenegro", "crna gora"),
    "czarnogora": ("czarnogora", "montenegro", "crna gora"),
    "francja": ("francja", "france"),
    "emiraty": ("emiraty", "united arab emirates", "zjednoczone emiraty arabskie",
                "uae"),
}


# ------------------------------------------------------------------ parsowanie

def parse_places(payload: dict) -> list[dict]:
    """Odpowiedź `places:searchText` -> lista kandydatów w formie roboczej.

    Świadomie spłaszczamy `displayName{text}` i `location{latitude,longitude}`,
    żeby reszta modułu nie musiała znać kształtu API. `rating` przeliczamy
    NA MIEJSCU do skali 0-10 (`normalize_to_10`, `bestRating=5`), bo nigdzie
    dalej nie chcemy mieć dwóch skal jednocześnie — to jest dokładnie ten typ
    pomyłki, który daje „hotel 4.2" obok „hotel 8.4".
    """
    out: list[dict] = []
    for p in ((payload or {}).get("places") or []):
        if not isinstance(p, dict) or not p.get("id"):
            continue
        name = p.get("displayName")
        if isinstance(name, dict):
            name = name.get("text")
        loc = p.get("location") or {}
        out.append({
            "id": str(p["id"]),
            "name": str(name or ""),
            "address": str(p.get("formattedAddress") or ""),
            "types": [str(t) for t in (p.get("types") or [])],
            "rating": normalize_to_10(p.get("rating"), GOOGLE_BEST_RATING),
            "rating_raw": _as_float(p.get("rating")),
            "review_count": _as_int(p.get("userRatingCount")),
            "lat": _as_float(loc.get("latitude")),
            "lng": _as_float(loc.get("longitude")),
        })
    return out


def parse_reviews(details: dict, limit: int = MAX_REVIEWS) -> list[str]:
    """Teksty opinii z `places/{id}` — najwyżej `MAX_REVIEWS`, bo tyle daje API.

    Bierzemy `text.text` (tłumaczenie na `languageCode`), a gdy go brak —
    `originalText.text`. Opinia bez treści (sama gwiazdka) jest pomijana:
    do niczego się nie nadaje, a zaśmieca listę.
    """
    out: list[str] = []
    for r in ((details or {}).get("reviews") or [])[:limit]:
        if not isinstance(r, dict):
            continue
        txt = ""
        for key in ("text", "originalText"):
            blok = r.get(key)
            if isinstance(blok, dict) and str(blok.get("text") or "").strip():
                txt = str(blok["text"]).strip()
                break
        if txt:
            out.append(" ".join(txt.split()))
    return out


def parse_error(payload: dict, status_code: int) -> str:
    """Komunikat błędu Google w postaci nadającej się do pokazania człowiekowi.

    API odpowiada `{"error": {"code": 400, "message": "...", "status":
    "INVALID_ARGUMENT"}}`. Najczęstszy realny błąd to brak `X-Goog-FieldMask`
    — dlatego komunikat wraca w całości, a nie jako samo „HTTP 400".
    """
    err = (payload or {}).get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "").strip()
        stat = str(err.get("status") or "").strip()
        if msg:
            return f"HTTP {status_code} {stat}: {msg}"[:300]
    return f"HTTP {status_code}"


# ----------------------------------------------------------------- dopasowanie

def is_lodging(types: Iterable[str]) -> bool:
    """Czy obiekt jest miejscem noclegowym.

    Google zwraca kilka typów naraz (`hotel`, `point_of_interest`,
    `establishment`), więc wystarczy jeden z listy noclegowej. Brak `types`
    w odpowiedzi traktujemy jako „nie wiadomo" i NIE odrzucamy — inaczej
    okrojony field mask cicho wyzerowałby całe źródło.
    """
    t = {str(x).lower() for x in (types or [])}
    if not t:
        return True
    return bool(t & LODGING_TYPES)


def country_matches(country: str, address: str) -> bool:
    """Czy `formattedAddress` z Google opisuje kraj z oferty.

    To jest ta sama lekcja, którą poprzednik opłacił na HolidayCheck:
    „Ambrosia (Athens)" z Aten dostaje „Hotel Ambrosia" w tureckim Bitez
    z podobieństwem nazwy 1.00. Sama nazwa kłamie; kraj nie.

    Sprawdzamy DWA OSTATNIE segmenty adresu, a nie cały ciąg. Google kończy
    `formattedAddress` krajem, a szukanie po całości dałoby trafienie na
    ulicy „Grecka" albo w dzielnicy „Malta" (taka jest w Poznaniu).
    """
    a = _strip_accents((address or "").lower())
    segmenty = [s.strip() for s in a.split(",") if s.strip()]
    ogon = " , ".join(segmenty[-2:]) if segmenty else a
    klucz = (country or "").lower().strip()
    aliasy = COUNTRY_ALIASES.get(klucz, (klucz,))
    return any(_strip_accents(x) in ogon for x in aliasy if x)


def place_matches(city: str, region: str, address: str) -> bool:
    """Czy miasto albo region z oferty pojawia się w adresie Google.

    To NIE jest warunek konieczny, tylko podnoszenie pewności — dokładnie jak
    w `external_ratings.place_agreement`. Adres bywa rozjechany zupełnie
    legalnie: Novotel Malta „Sliema" stoi administracyjnie w Gzirze.
    """
    a = _strip_accents((address or "").lower())
    for value in (city, region):
        v = _strip_accents((value or "").lower().strip())
        if len(v) >= 3 and v in a:
            return True
    return False


def pick_place(hotel_name: str, country: str, city: str, region: str,
               places: Iterable[dict]) -> tuple[dict | None, float, str]:
    """Wybiera hotel z listy Google i orzeka o pewności trafienia.

    Cztery sita, w tej kolejności — każde odsiewa inny rodzaj pomyłki:

    1. **TYP.** Kandydat bez typu noclegowego odpada. To sito jest możliwe
       dopiero dzięki Google (HolidayCheck nie ma czego takiego) i za darmo
       usuwa restauracje, bary i biura podróży noszące nazwę hotelu —
       na Krecie „Alkyonides" to również taverna.
    2. **KRAJ.** Warunek konieczny, weryfikowany przez `formattedAddress`.
    3. **NAZWA.** Próg `MATCH_THRESHOLD`; bez potwierdzenia miejsca wymagamy
       nazwy praktycznie identycznej (`STRICT_NAME_THRESHOLD`).
    4. **REGUŁA RYWALA.** Jeżeli w tym samym miejscu jest DRUGI kandydat
       równie zgodny z naszą nazwą (`is_compatible`), orzekamy `ambiguous`
       i oceny NIE używamy. Lepszy brak danych niż zła liczba: podstawiona
       ocena obcego hotelu zapaliłaby fałszywą flagę rozbieżności i kazała
       odrzucić dobry hotel.

    Zwraca `(kandydat, pewność 0-1, status)`.

    PREMIA ZA MIEJSCOWOŚĆ JEST TU DUŻO WIĘKSZA NIŻ PRZY HOLIDAYCHECK (0.30
    zamiast 0.05) i to nie jest kosmetyka. Google szuka po całym świecie,
    a normalizacja nazw zjada słowa generyczne — przez co
    `Alkyonides Hotel Apartments` w Stalidzie na Krecie ma po normalizacji
    nazwę IDENTYCZNĄ (1.00) z naszym `Alkyonides (Kremasti)`, podczas gdy
    właściwy `Alkyonides Boutique Hotel` w Kremasti dostaje 0.88. Ze słabą
    premią wygrywałby hotel z niewłaściwej wyspy. Premia wpływa wyłącznie
    na KOLEJNOŚĆ; zwracana pewność to nadal samo podobieństwo nazwy, a próg
    trafienia jest ten sam — kandydat z dobrego miasta, ale z byle jaką
    nazwą, dalej kończy jako `ambiguous`.
    """
    oceny: list[tuple[float, float, bool, dict]] = []
    for cand in places:
        if not is_lodging(cand.get("types")):
            continue
        if not country_matches(country, cand.get("address", "")):
            continue
        place_ok = place_matches(city, region, cand.get("address", ""))
        sim = name_similarity(hotel_name, cand.get("name", ""))
        oceny.append((sim + (PLACE_BONUS if place_ok else 0.0), sim, place_ok, cand))

    if not oceny:
        return None, 0.0, ST_NO_MATCH
    oceny.sort(key=lambda x: x[0], reverse=True)
    _, best_sim, best_place, best = oceny[0]

    pewne = best_sim >= MATCH_THRESHOLD and (best_place or best_sim >= STRICT_NAME_THRESHOLD)
    if pewne:
        rywale = [c for _, _, p, c in oceny[1:]
                  if p >= best_place and is_compatible(hotel_name, c.get("name", ""))]
        if rywale:
            pewne = False
    return best, best_sim, (ST_OK if pewne else ST_AMBIGUOUS)


def to_rating(hotel_id: str, place: dict, confidence: float) -> ExternalRating:
    """Kandydat Google -> `ExternalRating` w skali 0-10.

    Hotel, który jest w Google, ale nie ma ani jednej oceny, dostaje
    `no_rating` — to normalny, TRWAŁY stan (wart cache'owania), a nie awaria.
    """
    out = ExternalRating(
        hotel_id=str(hotel_id), source=SOURCE,
        matched_name=place.get("name", ""),
        rating=place.get("rating"),
        review_count=place.get("review_count"),
        url=MAPS_URL.format(place_id=place.get("id", "")),
        confidence=round(confidence, 3),
        status=ST_OK if place.get("rating") is not None else ST_NO_RATING,
    )
    return out


# ------------------------------------------------------------------ pobieranie

class GooglePlacesRatings:
    """Klient Google Places API (New). NIGDY nie rzuca przy błędzie sieci.

    BRAK KLUCZA TO NIE BŁĄD. Dopóki `GOOGLE_PLACES_API_KEY` nie jest
    ustawiony, `available` jest `False`, `fetch` oddaje status `no_key`
    bez tknięcia sieci, a `hs opinie` pokazuje w kolumnie Google „brak klucza"
    i działa dalej. Gdy klucz się pojawi — źródło rusza samo, bez zmiany kodu
    i bez migracji: `no_key` jest statusem NIETRWAŁYM, więc cache go nie
    zapamiętuje (patrz `external_ratings.TRANSIENT_STATUSES`).
    """

    name = SOURCE

    def __init__(self, api_key: str | None = None, delay: float = 0.2,
                 timeout: float = 20.0, http: httpx.Client | None = None,
                 language: str = "pl"):
        self.api_key = api_key if api_key is not None else config.get_secret(API_KEY_NAME)
        self.delay = delay
        self.language = language
        self._http = http
        self._timeout = timeout
        self._last_call = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _throttle(self) -> None:
        if not self.delay:
            return
        gap = time.monotonic() - self._last_call
        if self._last_call and gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    def _headers(self, field_mask: str) -> dict[str, str]:
        """Nagłówki Google. `X-Goog-FieldMask` jest OBOWIĄZKOWY — bez niego
        API nie ma listy domyślnej i odpowiada błędem `INVALID_ARGUMENT`."""
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key or "",
            "X-Goog-FieldMask": field_mask,
        }

    def search_text(self, query: str, max_results: int = 5,
                    lat: float | None = None, lng: float | None = None,
                    radius_m: float = 30000.0) -> tuple[list[dict], str]:
        """`places:searchText` -> `(kandydaci, błąd)`.

        Pusta lista bez błędu i pusta lista z błędem to dwie różne rzeczy:
        pierwsza jest faktem trwałym („tego hotelu Google nie zna"), druga
        stanem chwilowym. Gdyby jedno przebrało się za drugie, jedna awaria
        sieci zapisałaby cały ranking jako `no_match` na stałe.

        `locationBias` (okrąg wokół współrzędnych) doklejamy TYLKO wtedy,
        gdy współrzędne skądś mamy. API ofert wakacje.pl ich nie udostępnia,
        więc w praktyce zwykle ich nie ma — ale gdy hotel jest już w Google
        znaleziony, jego `location` można podać przy sąsiednich obiektach.
        """
        if not self.available:
            return [], f"brak klucza {API_KEY_NAME}"
        body: dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": max_results,
            "languageCode": self.language,
        }
        if lat is not None and lng is not None:
            body["locationBias"] = {"circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_m,
            }}

        self._throttle()
        try:
            r = self.http.post(SEARCH_URL, content=json.dumps(body),
                               headers=self._headers(SEARCH_FIELD_MASK))
            payload = r.json() if r.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            return [], f"sieć: {exc}"
        if r.status_code != 200:
            return [], parse_error(payload if isinstance(payload, dict) else {}, r.status_code)
        return parse_places(payload if isinstance(payload, dict) else {}), ""

    def details(self, place_id: str,
                field_mask: str = DETAILS_FIELD_MASK) -> tuple[dict, str]:
        """`GET places/{id}` -> `(słownik, błąd)`.

        Używane tylko po opinie: sama ocena i liczba opinii są już
        w odpowiedzi wyszukiwania, więc domyślna ścieżka to JEDNO żądanie
        na hotel. `reviews` w masce przenosi żądanie do najdroższego SKU.
        """
        if not self.available:
            return {}, f"brak klucza {API_KEY_NAME}"
        self._throttle()
        try:
            r = self.http.get(DETAILS_URL.format(place_id=place_id),
                              headers=self._headers(field_mask))
            payload = r.json() if r.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            return {}, f"sieć: {exc}"
        if r.status_code != 200:
            return {}, parse_error(payload if isinstance(payload, dict) else {}, r.status_code)
        return (payload if isinstance(payload, dict) else {}), ""

    def fetch(self, hotel_id: str, hotel_name: str, country: str = "",
              city: str = "", region: str = "", lat: float | None = None,
              lng: float | None = None, with_reviews: bool = False) -> ExternalRating:
        """Pełna droga: nazwa -> kandydaci -> dopasowanie -> ocena 0-10.

        Sygnatura jest zgodna z `HolidayCheckRatings.fetch`, żeby
        `get_or_fetch` obsługiwał oba źródła bez rozgałęzień.
        """
        now = datetime.now().isoformat(timespec="seconds")
        if not self.available:
            return ExternalRating(hotel_id=str(hotel_id), source=SOURCE,
                                  status=ST_NO_KEY, fetched_at=now,
                                  error=f"brak klucza {API_KEY_NAME}")

        query = _query(hotel_name, country, city, region)
        if not query:
            return ExternalRating(hotel_id=str(hotel_id), source=SOURCE,
                                  status=ST_NO_MATCH, fetched_at=now,
                                  error="pusta nazwa hotelu")

        cands, blad = self.search_text(query, lat=lat, lng=lng)
        if blad:
            return ExternalRating(hotel_id=str(hotel_id), source=SOURCE,
                                  status=ST_ERROR, fetched_at=now, error=blad)
        if not cands:
            return ExternalRating(hotel_id=str(hotel_id), source=SOURCE,
                                  status=ST_NO_MATCH, fetched_at=now,
                                  error="brak kandydatów")

        best, conf, status = pick_place(hotel_name, country, city, region, cands)
        if best is None or status == ST_AMBIGUOUS:
            return ExternalRating(
                hotel_id=str(hotel_id), source=SOURCE,
                matched_name=(best or {}).get("name", ""), confidence=round(conf, 3),
                status=(ST_NO_MATCH if best is None else ST_AMBIGUOUS),
                url=MAPS_URL.format(place_id=best["id"]) if best else "",
                fetched_at=now,
            )

        out = to_rating(hotel_id, best, conf)
        out.fetched_at = now
        if with_reviews:
            szczegoly, blad = self.details(best["id"])
            if not blad:
                out.reviews = parse_reviews(szczegoly)
        return out


def _query(hotel_name: str, country: str, city: str, region: str) -> str:
    """Fraza `textQuery`: „<nazwa> <miasto> <kraj>".

    Nazwę czyścimy tą samą funkcją co przy HolidayCheck (`search_query`) —
    zdejmuje nawiasy (`Alkyonides (Kremasti)` -> `Alkyonides`) i ogon po `ex.`,
    a miasto dokleja tylko wtedy, gdy nie ma go już w nazwie. Bez tego wychodzi
    „Alkyonides (Kremasti) Kremasti", czyli fraza, w której miasto waży dwa
    razy więcej niż nazwa hotelu.

    Kraj dopisujemy na końcu i to jest różnica wobec HolidayCheck: tam kraj
    wynikał z tenanta (`hcde`), tu Google szuka po całym świecie i bez kraju
    chętnie odda hotel o tej samej nazwie z innego kontynentu.
    """
    # Hotel bez nazwy, ale z miastem, dałby frazę „Kremasti Grecja" — czyli
    # zapytanie o PRZYPADKOWY obiekt w tej miejscowości, które Google chętnie
    # spełni i odda coś z oceną. `search_query` sam tego nie odsieje, bo miasto
    # zostaje; dlatego sprawdzamy nazwę osobno, ZANIM cokolwiek doklejamy.
    if not normalize_name(hotel_name):
        return ""
    baza = search_query(hotel_name, city, region)
    if not baza:
        return ""
    kraj = (country or "").strip()
    if kraj and _strip_accents(kraj.lower()) not in _strip_accents(baza.lower()):
        baza = f"{baza} {kraj}"
    return baza.strip()


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    f = _as_float(v)
    return int(f) if f is not None else None
