"""Weryfikacja ceny końcowej ofert wakacje.pl.

PROBLEM
-------
Listing (`search.tripsSearch`) zwraca cenę „od". Cały ranking cenowy projektu
stoi na założeniu, że ta cena jest prawdziwa — a nikt tego nie sprawdził.
Ten moduł sprawdza ją empirycznie, ofertę po ofercie.

REKONESANS (pełny zapis: docs/weryfikacja-ceny.md)
--------------------------------------------------
* `POST /v2/api/getOfferDataFromMerlin` — DZIAŁA (payload z zadania jest
  poprawny), ale zwraca WYŁĄCZNIE atrybuty hotelu (plaża, nurkowanie, wi-fi).
  Zero ceny. Ślepy zaułek dla weryfikacji.
* `POST /v2/api/getCalculatorOfferVariants/{offerId}` — TO JEST TEN ENDPOINT.
  W fazie 0 zapisano go bez segmentu `{offerId}`, stąd wcześniejsze 404.
  Ciało jest PŁASKIE (nie `{query:…}`), a odpowiedź to lista realnych,
  rezerwowalnych wariantów pokoi z ceną.
* `GET /v2/api/getInitOfferData/{offerId}/?duration&startsAt&departureId` —
  jednym strzałem oddaje komplet parametrów potrzebnych kalkulatorowi
  (hotelId, tourOpCode, tourId, serviceTypeId, transportId) ORAZ świeżą cenę
  listingową. Dzięki temu odróżniamy „listing kłamie" od „nasz snapshot
  jest nieaktualny".

KLUCZOWE USTALENIE — `totalPrice` JEST ZA CAŁY POKÓJ, NIE ZA OSOBĘ
------------------------------------------------------------------
Zweryfikowane przez zmianę liczby dorosłych na tej samej ofercie (Elios,
offerId 1091524): 1 os. = 2550, 2 os. = 4094, 3 os. = 5830. Gdyby cena była
za osobę, wartość nie rosłaby liniowo z obłożeniem. Potwierdza to również
`getOfferAvailability`, które zwraca wprost `isPerPerson: false`.
Dlatego dzielimy przez liczbę osób — inaczej każda oferta wyszłaby
„zawyżona o 100%", co byłoby fałszywym alarmem wbudowanym w narzędzie.

WYNIK: CENA Z LISTINGU JEST PRAWDZIWA
-------------------------------------
Na 7 losowych ofertach z bazy najtańszy wariant kalkulatora / liczba osób
zgadzał się z ceną listingową co do złotówki (diff 0.0%). „Od" oznacza więc
najtańszy REALNY pokój, a nie cenę-wabik. Dopłata pojawia się dopiero, gdy
chcesz lepszy pokój — i potrafi być duża (Asteria Family Resort Side:
2851 → 4383 zł/os, +54%). Dlatego weryfikacja raportuje też `max_pp`:
sam rozrzut wariantów jest informacją, nawet gdy cena „od" się broni.

CZEGO NIE UDAŁO SIĘ USTALIĆ
---------------------------
`POST /v2/api/getCrossell` (bagaż, transfer, parking) odpowiada
`{"success": false, "data": null}` bez treści błędu dla każdego zgadywanego
ciała — patrz `docs/weryfikacja-ceny.md`. To jednak dodatki OPCJONALNE,
więc nie wchodzą do ceny obowiązkowej i nie blokują weryfikacji. Jedyny
sygnał o bagażu, jaki mamy, to pole `isLuggageIncluded` przy wariancie.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

BASE = "https://www.wakacje.pl"
API = f"{BASE}/v2/api"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Progi zgodności ceny. „Zgodna" z tolerancją, bo kalkulator potrafi zaokrąglić
# inaczej niż listing przy dzieleniu ceny pokoju przez liczbę osób.
TOLERANCE_PCT = 2.0      # <= tyle: cena z listingu się broni
SUSPICIOUS_PCT = 10.0    # powyżej: cena „od" realnie wprowadza w błąd

VERIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_verification (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key     TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    listing_price INTEGER,
    final_price   INTEGER,
    diff_pct      REAL,
    details_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_verif_offer
    ON price_verification(offer_key, checked_at);
"""

# offerId siedzi w deep-linku: /oferty/{slug}-{offerId}.html?...
# Kolumna `raw_id` nie trafiła do schematu tabeli `offer`, więc URL jest
# jedynym miejscem, z którego da się go odzyskać dla już zapisanych ofert.
_OFFER_ID_RE = re.compile(r"-(\d+)\.html")

# Kody lotnisk -> departureId (kopia z providers/wakacje.py; moduł weryfikacji
# ma nie zależeć od providera, żeby dało się go testować bez sieci).
DEPARTURES = {
    "BZG": 269, "GDN": 2880, "KTW": 2622, "KRK": 2696, "LUZ": 272, "LCJ": 2654,
    "SZY": 2891, "POZ": 2632, "RZE": 1909, "SZZ": 2904, "WAW": 278,
    "RDO": 11688, "WRO": 256, "IEG": 306, "BER": 3301, "DRS": 10305, "OSR": 4837,
    "WMI": 278,
}

SERVICE_CODES = {
    "AI": 1, "HB": 2, "BB": 3, "OWN": 4, "PROGRAM": 5,
    "FB": 6, "SNACK": 7, "DINNER": 8, "UAI": 9, "AI_SOFT": 10, "AI_PLUS": 44,
}


# ---------------------------------------------------------------- struktury


@dataclass
class RoomVariant:
    """Jeden rezerwowalny wariant pokoju z kalkulatora."""
    room_desc: str = ""
    total_price: int = 0             # ZA CAŁY POKÓJ (wszystkie osoby)
    price_pp: int = 0                # po podzieleniu przez liczbę osób
    features: list[str] = field(default_factory=list)   # roomDescAdditional
    luggage_included: Optional[bool] = None
    tour_op: str = ""
    currency: str = "PLN"


@dataclass
class Verification:
    """Wynik weryfikacji jednej oferty. NIGDY nie rzuca — brak danych zapisuje
    się w `error`, bo oferta wyprzedana to normalny stan świata, nie awaria."""
    offer_key: str
    hotel_name: str = ""
    departure_date: str = ""
    nights: int = 0
    adults: int = 2
    offer_id: str = ""
    listing_price: Optional[int] = None       # z naszej bazy (price_snapshot)
    api_listing_price: Optional[int] = None   # świeża cena „od" z getInitOfferData
    final_price: Optional[int] = None         # najtańszy wariant / os.
    max_price: Optional[int] = None           # najdroższy wariant / os.
    variants: list[RoomVariant] = field(default_factory=list)
    error: Optional[str] = None
    checked_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return self.error is None and self.final_price is not None

    @property
    def diff_pln(self) -> Optional[int]:
        if not self.ok or not self.listing_price:
            return None
        return int(self.final_price) - int(self.listing_price)

    @property
    def diff_pct(self) -> Optional[float]:
        if not self.ok or not self.listing_price:
            return None
        return (self.final_price - self.listing_price) / self.listing_price * 100.0

    @property
    def diff_vs_api_pct(self) -> Optional[float]:
        """Cena końcowa vs ŚWIEŻA cena „od" z API. To jest właściwa miara
        uczciwości listingu; `diff_pct` miesza ją z wiekiem naszego snapshotu."""
        return diff_percent(self.api_listing_price, self.final_price)

    @property
    def stale_snapshot(self) -> bool:
        """Rozjazd bierze się z nieaktualnej bazy, nie z ceny-wabika:
        świeży listing zgadza się z ceną końcową, a różni się od naszego."""
        api = self.diff_vs_api_pct
        return (api is not None and abs(api) <= TOLERANCE_PCT
                and self.api_listing_price != self.listing_price)

    @property
    def verdict(self) -> str:
        """zgodna | nieaktualna | odchylenie | zawyzona | nieznana

        `nieaktualna` to osobna kategoria celowo: oznacza, że listing NIE
        kłamał — po prostu cena zmieniła się od naszego ostatniego przebiegu.
        Wrzucenie tego do „zawyżona" oskarżałoby serwis o czyjś cudzy problem
        i zafałszowało odpowiedź na pytanie, po co ten moduł powstał.
        """
        pct = self.diff_pct
        if pct is None:
            return "nieznana"
        if abs(pct) <= TOLERANCE_PCT:
            return "zgodna"
        if self.stale_snapshot:
            return "nieaktualna"
        if abs(pct) <= SUSPICIOUS_PCT:
            return "odchylenie"
        return "zawyzona"

    @property
    def note(self) -> str:
        """Uwagi dla człowieka — to, czego sama liczba nie powie."""
        if self.error:
            return self.error
        bits: list[str] = []
        if self.variants:
            bits.append(f"{len(self.variants)} wariant(ów) pokoi")
        # Rozrzut wariantów: cena „od" bywa poprawna, ale dotyczy najgorszego pokoju.
        if (self.max_price and self.final_price
                and self.max_price > self.final_price):
            up = (self.max_price - self.final_price) / self.final_price * 100.0
            bits.append(f"lepszy pokój do +{up:.0f}%")
        # Rozjazd naszego snapshotu ze świeżą ceną „od" to NIE jest kłamstwo
        # listingu, tylko nieaktualna baza — rozróżniamy te dwie rzeczy.
        if (self.api_listing_price and self.listing_price
                and self.api_listing_price != self.listing_price):
            what = ("listing zmienił się na "
                    if self.stale_snapshot else "świeży listing: ")
            bits.append(f"{what}{self.api_listing_price} zł/os")
        if self.variants and all(v.luggage_included is False for v in self.variants):
            bits.append("bagaż płatny osobno")
        return "; ".join(bits)

    def details(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "hotel_name": self.hotel_name,
            "departure_date": self.departure_date,
            "nights": self.nights,
            "adults": self.adults,
            "api_listing_price": self.api_listing_price,
            "max_price_pp": self.max_price,
            "verdict": self.verdict,
            "note": self.note,
            "error": self.error,
            "variants": [asdict(v) for v in self.variants],
        }


# ---------------------------------------------------------- czyste funkcje


def offer_id_from_url(url: str) -> str:
    """`/oferty/elios-1091524.html?od-…` -> `1091524`. Pusty string, gdy URL
    jest linkiem do hotelu (`/hotele/...`), a nie do konkretnej oferty —
    takiej pozycji nie da się zweryfikować."""
    if not url or "/oferty/" not in url:
        return ""
    m = _OFFER_ID_RE.search(url)
    return m.group(1) if m else ""


def build_calculator_payload(init: dict[str, Any], adults: int, departure_id: int,
                             nights: int = 0, departure_date: str = "",
                             departure_code: str = "") -> dict[str, Any]:
    """Płaskie ciało dla `getCalculatorOfferVariants/{offerId}`.

    Kształt odtworzony z bundla `_app-*.js` — tam parametry lecą z pola
    `this.params` sklejonego przez konfigurator. Wartości z `getInitOfferData`
    mają pierwszeństwo nad tym, co mamy w bazie: baza może być o kilka dni
    stara, a kalkulator odrzuci niespójny komplet (tourOp + hotelId + tourId).
    """
    return {
        "adults": adults,
        "kids": 0,
        "serviceId": init.get("serviceTypeId"),
        "infants": 0,
        "duration": init.get("nights") or init.get("currentDuration") or nights,
        "kidsAges": [],
        "departureDate": init.get("departureDate") or departure_date,
        "transportId": init.get("transportId") or 1,
        "departureCityId": departure_id,
        "departureCityCode": init.get("departure") or departure_code,
        "hotelId": init.get("hotelId"),
        "tourOp": init.get("tourOpCode"),
        "tourId": init.get("tourId"),
        "cruiseId": init.get("cruiseId") or None,
        "roundTripId": init.get("roundTripId") or None,
        "isAlternativeRoom": False,
        "isOffer77": False,
    }


def parse_variants(body: dict[str, Any], adults: int) -> list[RoomVariant]:
    """Wyciąga warianty pokoi z odpowiedzi kalkulatora.

    `totalPrice` jest ZA CAŁY POKÓJ (patrz docstring modułu), więc dzielimy
    przez liczbę osób — reszta projektu liczy wyłącznie w zł/os.
    """
    per = max(1, int(adults or 1))
    out: list[RoomVariant] = []
    for raw in (body.get("offers") or []):
        if not isinstance(raw, dict):
            continue
        total = _as_int(raw.get("totalPrice"))
        if total is None:
            total = _as_int(raw.get("basePrice"))
        if not total:
            continue
        out.append(RoomVariant(
            room_desc=str(raw.get("roomDesc") or "").strip(),
            total_price=total,
            price_pp=int(round(total / per)),
            features=[str(x) for x in (raw.get("roomDescAdditional") or [])],
            luggage_included=_as_bool(raw.get("isLuggageIncluded")),
            tour_op=str(raw.get("tourOp") or raw.get("providerCode") or ""),
            currency=str(raw.get("priceCurrency") or "PLN"),
        ))
    out.sort(key=lambda v: v.total_price)
    return out


def diff_percent(listing: Optional[int], final: Optional[int]) -> Optional[float]:
    """Różnica ceny końcowej względem listingowej, w procentach.
    None, gdy któregokolwiek składnika brakuje albo listing to 0
    (dzielenie przez zero to brak danych, nie nieskończone zawyżenie)."""
    if not listing or final is None:
        return None
    return (final - listing) / listing * 100.0


# --------------------------------------------------------------- pobieranie


class PriceVerifier:
    """Odpytuje wakacje.pl o realne warianty pokoi dla konkretnej oferty.

    Wzorowane na `ai.opinions.WakacjeOpinions`: klient HTTP jest leniwy
    i wstrzykiwalny (`http=`), żeby testy mogły podstawić `httpx.MockTransport`
    bez ruszania sieci. Metoda `verify` nigdy nie rzuca.
    """

    name = "wakacje.pl"

    def __init__(self, delay: float = 1.5, timeout: float = 45.0,
                 retries: int = 2, http: httpx.Client | None = None):
        self.delay = delay
        self.retries = retries
        self._http = http
        self._timeout = timeout
        self._last_call = 0.0

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self._timeout,
                headers={
                    "User-Agent": UA,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": f"{BASE}/",
                    "Origin": BASE,
                },
            )
        return self._http

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if self._last_call and gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    def _call(self, method: str, url: str,
              payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Zwraca `data` z koperty {success, data}. Rzuca RuntimeError —
        łapie to dopiero `verify`, żeby zamienić błąd na wpis „nie udało się".

        Retry 2× tylko na błędach sieci. Logicznego `success: false` NIE
        powtarzamy: to trwała odpowiedź serwisu (oferty nie ma), a nie usterka
        łącza — ponawianie tylko dobijałoby cudzy serwer bez szansy na inny wynik.
        """
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                if method == "GET":
                    r = self.http.get(url)
                else:
                    r = self.http.post(url, content=json.dumps(payload or {}))
                r.raise_for_status()
                body = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(self.delay * (2 ** (attempt + 1)))
                continue
            if not body.get("success"):
                raise RuntimeError(_error_text(body))
            return body.get("data") or {}
        raise RuntimeError(f"sieć: {last}")

    def fetch_init(self, offer_id: str, nights: int, departure_date: str,
                   departure_id: int) -> dict[str, Any]:
        url = (f"{API}/getInitOfferData/{offer_id}/"
               f"?duration={nights}&startsAt={departure_date}&departureId={departure_id}")
        return self._call("GET", url)

    def fetch_variants(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{API}/getCalculatorOfferVariants/{offer_id}", payload)

    # ---------- główne wejście ----------

    def verify(self, offer: dict[str, Any], adults: int = 2) -> Verification:
        """`offer` to wiersz z bazy (albo dowolny mapping) z kluczami:
        key, hotel_name, departure_date, nights, departure_code, url, price."""
        v = Verification(
            offer_key=str(offer.get("key") or ""),
            hotel_name=str(offer.get("hotel_name") or ""),
            departure_date=str(offer.get("departure_date") or ""),
            nights=int(offer.get("nights") or 0),
            adults=adults,
            listing_price=_as_int(offer.get("price")),
        )
        v.offer_id = offer_id_from_url(str(offer.get("url") or ""))
        if not v.offer_id:
            v.error = "brak offerId w URL (link do hotelu, nie do oferty)"
            return v

        dep_code = str(offer.get("departure_code") or "")
        departure_id = DEPARTURES.get(dep_code, DEPARTURES["WAW"])

        try:
            init = self.fetch_init(v.offer_id, v.nights, v.departure_date, departure_id)
        except RuntimeError as exc:
            v.error = f"getInitOfferData: {exc}"
            return v
        if not init:
            v.error = "getInitOfferData: pusta odpowiedź (oferta wycofana?)"
            return v
        v.api_listing_price = _as_int(init.get("price"))

        payload = build_calculator_payload(
            init, adults, departure_id, nights=v.nights,
            departure_date=v.departure_date, departure_code=dep_code,
        )
        try:
            data = self.fetch_variants(v.offer_id, payload)
        except RuntimeError as exc:
            v.error = f"getCalculatorOfferVariants: {exc}"
            return v

        v.variants = parse_variants(data, adults)
        if not v.variants:
            # Pusta lista wariantów przy success:true to konkretna informacja:
            # termin/pokój wyprzedany. Nie jest to błąd techniczny.
            v.error = "brak dostępnych wariantów pokoi (termin wyprzedany?)"
            return v

        v.final_price = v.variants[0].price_pp
        v.max_price = v.variants[-1].price_pp
        return v


# ------------------------------------------------------------------- baza


def ensure_schema(db) -> None:
    """Idempotentne — wołane przy każdym użyciu, tak jak deals.ensure_schema."""
    db.executescript(VERIFICATION_SCHEMA)
    db.commit()


def save_verification(db, v: Verification) -> None:
    ensure_schema(db)
    db.execute(
        """INSERT INTO price_verification
           (offer_key, checked_at, listing_price, final_price, diff_pct, details_json)
           VALUES (?,?,?,?,?,?)""",
        (v.offer_key, v.checked_at, v.listing_price, v.final_price,
         None if v.diff_pct is None else round(v.diff_pct, 2),
         json.dumps(v.details(), ensure_ascii=False)),
    )
    db.commit()


def offers_to_verify(db, profile: str, top: int = 8) -> list[dict[str, Any]]:
    """Najtańsze oferty profilu wraz z NAJNOWSZYM snapshotem ceny.

    Profil wiążemy z ofertą przez `run` -> `price_snapshot.run_id` (tak jak
    robi to `deals.diff_between_runs`); w tabeli `offer` nie ma kolumny profilu.
    Weryfikujemy tylko wakacje.pl — kalkulator r.pl to inny, nieprzebadany
    endpoint, a cicho podstawiona cena z innego źródła byłaby gorsza niż jej brak.
    """
    rows = db.execute(
        """
        SELECT o.key, o.hotel_name, o.region, o.city, o.departure_date, o.nights,
               o.board, o.departure_code, o.url, s.price, s.ts
        FROM offer o
        JOIN price_snapshot s ON s.id = (
            SELECT MAX(p.id) FROM price_snapshot p WHERE p.offer_key = o.key
        )
        WHERE o.provider = 'wakacje.pl'
          AND o.key IN (
            SELECT ps.offer_key FROM price_snapshot ps
            JOIN run r ON r.id = ps.run_id
            WHERE r.profile = ?
          )
        ORDER BY s.price ASC
        LIMIT ?
        """,
        (profile, int(top)),
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ drobiazgi


def _error_text(body: dict[str, Any]) -> str:
    err = body.get("errors") or body.get("error") or body.get("msg")
    if isinstance(err, dict):
        err = err.get("message") or err
    if isinstance(err, list):
        err = "; ".join(str(e) for e in err)
    text = str(err or "odpowiedź bez treści błędu").strip()
    # Serwis potrafi odesłać dosłownie "⛔️ undefined" dla nieistniejącej oferty.
    return "oferta nieznana serwisowi" if "undefined" in text else text


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_bool(v: Any) -> Optional[bool]:
    return None if v is None else bool(v)
