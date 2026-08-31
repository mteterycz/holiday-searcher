"""Drugie źródło opinii o hotelach — HolidayCheck.

PO CO TO JEST
-------------
wakacje.pl pokazuje przy hotelu ocenę w skali 0-10, ale bardzo często liczy ją
z 1-9 opinii. „Ocena 10.0 z jednej opinii" to nie jakość hotelu, tylko szum:
jeden zadowolony gość ustawia hotel wyżej niż 300 umiarkowanie zadowolonych.
Profil `wrzesien-okazje` filtruje oferty progiem `rating_min: 8.0`, więc ten
szum przechodzi przez filtr jako fakt. Ten moduł dokłada DRUGIE, niezależne
źródło oceny, żeby taką ocenę potwierdzić albo zdemaskować.

Empirycznie (patrz docs/opinie-zewnetrzne.md): w bazie 24 z 60 hoteli ma ≤9
opinii, a „Alkyonides (Kremasti) — 10.0 z 1 opinii" ma na HolidayCheck 6.2 z 4
opinii. Ten moduł ma takie przypadki wyłapywać, a nie uśredniać.

SKĄD DANE (rekonesans: docs/opinie-zewnetrzne.md)
------------------------------------------------
Sprawdzone i ODRZUCONE: Booking.com (challenge AWS WAF, wymaga JS),
Google Places (twardy `REQUEST_DENIED — you must use an API key`),
Google Maps/Search (ściana zgody + render po stronie klienta),
TripAdvisor (403 + captcha). Pole `holidayCheckRate` w `getOpinionsBox`
wakacje.pl jest zawsze `null` — skrótu nie ma.

Wybrane: HolidayCheck, dwa kroki, oba bez logowania, klucza i przeglądarki.

1. NAZWA -> ID. `POST https://www.holidaycheck.de/svc/content-query-v2`
   (otwarty GraphQL, bez auth), `suggestionSearch(query, limit, tenant, type)`.
   Zwraca `id` (UUID), `name` i `placeDetailString` w postaci
   „Hotel in Kremasti, Rhodos, Griechenland" — czyli komplet do weryfikacji
   miejsca. UWAGA: `type` to `[String]`, nie `String`.
2. ID -> OCENA. `GET https://www.holidaycheck.de/hi/x/<uuid>` przekierowuje na
   kanoniczny adres hotelu, a ta strona jest renderowana PO STRONIE SERWERA
   i zawiera `<script type=application/ld+json>` ze schema.org `Hotel`:
   `aggregateRating {ratingValue, ratingCount, bestRating, worstRating}`
   plus tablicę `review[]` z treściami. Slug jest niepotrzebny — wystarczy UUID.

Świadomie NIE używamy `hotelOfferSearch` (ma pola ocen, ale zwraca wyłącznie
hotele z aktywną ofertą sprzedażową — dla samego `hotel_id` oddaje 0 pozycji).

DLACZEGO DOPASOWANIE NAZW MUSI SPRAWDZAĆ MIEJSCE
------------------------------------------------
Sama podobna nazwa kłamie. „Ambrosia (Athens)" z bazy dostaje z HolidayCheck
„Hotel Ambrosia" w Bitez w TURCJI z podobieństwem nazwy 1.00. Dlatego kraj
z oferty jest warunkiem KONIECZNYM trafienia, a miasto/region podnoszą pewność.
Dopasowanie, które nie przejdzie progu, dostaje status `ambiguous`, jest
zapisywane w cache'u (żeby nie pytać drugi raz) i NIE jest używane do oceny.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import statistics
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

SOURCE = "holidaycheck"
GQL_URL = "https://www.holidaycheck.de/svc/content-query-v2"
HOTEL_URL = "https://www.holidaycheck.de/hi/x/{uuid}"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
# Akamai przepuszcza dopiero komplet nagłówków przeglądarki. Samo User-Agent
# daje HTTP 400 z AkamaiGHost — sprawdzone, patrz docs/opinie-zewnetrzne.md.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

SUGGEST_QUERY = """
query($q: String, $limit: Int) {
  suggestionSearch(query: $q, limit: $limit, tenant: "hcde", type: ["hotel"]) {
    hotels { count entities { id name placeDetailString } }
  }
}
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_external_rating (
    hotel_id     TEXT NOT NULL,
    source       TEXT NOT NULL,
    matched_name TEXT,
    rating_0_10  REAL,
    review_count INTEGER,
    url          TEXT,
    confidence   REAL,
    status       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (hotel_id, source)
);
"""

# Statusy wiersza cache'u. Każdy z nich jest ZAPISYWANY — także porażki,
# bo „nie ma tego hotelu na HolidayCheck" to trwały fakt, a nie chwilowy błąd,
# i nie ma sensu pytać o niego przy każdym uruchomieniu.
ST_OK = "ok"                 # trafienie pewne + jest ocena
ST_AMBIGUOUS = "ambiguous"   # znaleziono kandydata, ale za słabo pasuje -> NIE używać
ST_NO_MATCH = "no_match"     # nic sensownego nie znaleziono
ST_NO_RATING = "no_rating"   # hotel jest, ale nie ma ani jednej opinii
ST_ERROR = "error"           # sieć/parsowanie padło — status wart ponowienia
ST_NO_KEY = "no_key"         # źródło wymaga klucza API, którego jeszcze nie ma

# Statusy NIETRWAŁE — `ExternalRatingStore.get` traktuje je jak brak wpisu.
# `error` to padnięta sieć (jutro może działać), `no_key` to brak klucza API
# (jutro użytkownik może go wgrać). Zapamiętanie któregokolwiek na stałe
# wyłączyłoby źródło dla całego rankingu bez możliwości powrotu.
TRANSIENT_STATUSES = frozenset({ST_ERROR, ST_NO_KEY})

# Trafienie uznajemy za pewne dopiero od tego progu podobieństwa nazw.
MATCH_THRESHOLD = 0.80
# Bez potwierdzenia miasta/regionu wymagamy nazwy praktycznie identycznej.
STRICT_NAME_THRESHOLD = 0.97
# Rozjazd ocen powyżej tylu punktów to flaga „rozbieżność".
DIVERGENCE_PTS = 1.5
# Ocena oparta na tylu opiniach (lub mniej) jest statystycznie bezwartościowa.
THIN_EVIDENCE = 3

# HolidayCheck jest niemiecki, baza ofert polska. Nazwy miast bywają zbieżne
# (Kremasti, Letojanni, Pefkochori), nazwy KRAJÓW nigdy — a to właśnie kraj
# odsiewa „Hotel Ambrosia" w Turcji od „Ambrosia" w Atenach.
COUNTRY_PL_DE = {
    "grecja": "griechenland", "turcja": "türkei", "włochy": "italien",
    "wlochy": "italien", "hiszpania": "spanien", "malta": "malta",
    "cypr": "zypern", "portugalia": "portugal", "egipt": "ägypten",
    "chorwacja": "kroatien", "albania": "albanien", "bułgaria": "bulgarien",
    "tunezja": "tunesien", "maroko": "marokko", "czarnogóra": "montenegro",
    "czarnogora": "montenegro", "francja": "frankreich", "emiraty": "emirate",
}

# Słowa, które w nazwie hotelu nic nie rozróżniają. „Hotel Venus Beach" i
# „Venus Beach" to ten sam obiekt; „Venus Beach" i „Venus Garden" już nie —
# dlatego `beach`/`garden` NIE są na tej liście.
_GENERIC = re.compile(
    r"\b(hotel|hotels|resort|club|aparthotel|apartment|apartments|apartamenty"
    r"|studio|studios|the|by|and|und|amp)\b"
)
_LD_RE = re.compile(
    r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>(.*?)</script>', re.S
)


# ------------------------------------------------------------------ dopasowanie

def normalize_name(s: str) -> str:
    """Sprowadza nazwę hotelu do porównywalnego rdzenia.

    Wycina dopiski w nawiasach (baza lubi `Olympia (Pefkohori)`), ogon po
    `ex.` (`Kirbiyik Resort (ex. Dinler)`), słowa generyczne i znaki
    interpunkcyjne. Niemieckie umlauty i polskie ogonki spłaszczamy do ASCII,
    żeby `Türkei`/`Turcja` czy `Marmárion`/`Marmarion` nie rozjeżdżały się
    na samym kodowaniu.
    """
    s = (s or "").lower().replace("ß", "ss")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\bex\.?\s.*$", " ", s)
    s = _strip_accents(s)
    s = _GENERIC.sub(" ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def search_query(hotel_name: str, city: str = "", region: str = "") -> str:
    """Fraza do `suggestionSearch`. Nie jest to zwykłe sklejenie pól.

    Baza trzyma nazwy z dopiskiem miejsca — `Olympia (Pefkohori)` — a do tego
    doklejamy miasto z oferty. Wychodzi „Alkyonides (Kremasti) Kremasti",
    czyli fraza, w której miasto waży DWA RAZY więcej niż nazwa hotelu.
    Wyszukiwarka HolidayCheck odpowiada wtedy listą przypadkowych obiektów
    z tej miejscowości, a szukanego hotelu nie ma w niej wcale — sprawdzone
    empirycznie na Alkyonides (patrz docs/opinie-zewnetrzne.md).

    Dlatego: najpierw zdejmujemy z nazwy nawiasy i ogon po `ex.`, a potem
    dokładamy miasto TYLKO wtedy, gdy nie ma go już w nazwie.
    """
    baza = re.sub(r"\(.*?\)", " ", hotel_name or "")
    baza = re.sub(r"\bex\.?\s.*$", " ", baza, flags=re.I)
    baza = re.sub(r"\s+", " ", baza).strip()

    miejsce = (city or region or "").strip()
    if miejsce and _strip_accents(miejsce.lower()) in _strip_accents(baza.lower()):
        miejsce = ""
    return " ".join(x for x in [baza, miejsce] if x).strip()


def name_similarity(a: str, b: str) -> float:
    """Podobieństwo nazw w skali 0-1, oparte na `difflib.SequenceMatcher`.

    Sam SequenceMatcher jest za surowy dla realnych par: `Alkyonides` kontra
    `Alkyonides Boutique Hotel` daje 0.69, choć to bez wątpienia ten sam hotel —
    HolidayCheck po prostu trzyma pełniejszą nazwę. Dlatego, gdy po normalizacji
    KOMPLET słów krótszej nazwy zawiera się w dłuższej, podnosimy wynik do 0.88:
    powyżej progu trafienia, ale wyraźnie poniżej dopasowania dokładnego.

    To ustępstwo jest bezpieczne wyłącznie dlatego, że kraj i tak musi się
    zgadzać (patrz `place_agreement`) — bez tego `Olympia` pasowałaby do
    każdego „Olympia coś tam" na świecie.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta) and min(len(na), len(nb)) >= 5:
        ratio = max(ratio, 0.88)
    return round(ratio, 3)


def is_compatible(ours: str, theirs: str) -> bool:
    """Czy nazwa z HolidayCheck jest „naszą nazwą, tylko dokładniejszą".

    Prawda, gdy po normalizacji KOMPLET naszych słów siedzi w ich nazwie.
    To jest test na rywala, nie na trafienie: jeśli takich kandydatów
    w jednej miejscowości jest kilku, to znaczy, że nasza nazwa jest za uboga,
    by któregokolwiek wskazać.

    `Karbel` pasuje do `Hotel Karbel`, `Hotel Karbel Sun` ORAZ `Hotel Karbel
    Beach` — trzech różnych hoteli w Ölüdeniz. Za to `Club Cettia Resort`
    (po normalizacji samo „cettia") NIE jest zgodne z `Grand Cettia`, bo gubi
    słowo „grand" — a to inny obiekt, nie dokładniejszy zapis tego samego.
    """
    ta, tb = set(normalize_name(ours).split()), set(normalize_name(theirs).split())
    return bool(ta) and bool(tb) and ta <= tb


def place_agreement(country: str, city: str, region: str, place: str) -> tuple[bool, bool]:
    """Czy `placeDetailString` HolidayCheck opisuje to samo miejsce co oferta.

    `place` ma postać „Hotel in Kremasti, Rhodos, Griechenland".
    Zwraca `(kraj_się_zgadza, miasto_albo_region_się_zgadza)`.
    Kraj jest warunkiem koniecznym; miasto/region tylko podnosi pewność,
    bo bywa rozjechane legalnie (Novotel Malta *Sliema* ma na HolidayCheck
    adres w sąsiednim Gzira).
    """
    p = _strip_accents((place or "").lower())
    want = COUNTRY_PL_DE.get((country or "").lower().strip(), (country or "").lower().strip())
    country_ok = bool(want) and _strip_accents(want) in p
    parts = [t.strip() for t in re.split(r"[,/]", p) if t.strip()]

    def hit(value: str) -> bool:
        v = _strip_accents((value or "").lower().strip())
        if len(v) < 3:
            return False
        if v in p:
            return True
        return any(difflib.SequenceMatcher(None, v, t).ratio() >= 0.75 for t in parts)

    return country_ok, (hit(city) or hit(region))


# ------------------------------------------------------------------- parsowanie

@dataclass
class ExternalRating:
    """Ocena hotelu z zewnętrznego źródła. `rating` ZAWSZE w skali 0-10."""
    hotel_id: str = ""
    source: str = SOURCE
    matched_name: str = ""
    rating: Optional[float] = None
    review_count: Optional[int] = None
    url: str = ""
    confidence: float = 0.0
    status: str = ST_NO_MATCH
    fetched_at: str = ""
    reviews: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def usable(self) -> bool:
        """Tylko `ok` wolno użyć do oceny. `ambiguous` jest zapisane po to,
        by go NIE użyć — i by nie pytać o nie drugi raz."""
        return self.status == ST_OK and self.rating is not None


def normalize_to_10(value: Any, best: Any = 10, worst: Any = 1) -> Optional[float]:
    """Sprowadza ocenę do skali 0-10 wg `bestRating` z JSON-LD.

    HolidayCheck oddaje dziś `bestRating: "10"`, ale historycznie serwis
    używał skali 1-6 i część danych może jeszcze tak wyglądać — dlatego
    dzielimy przez realne `bestRating`, a nie przez zaszytą stałą.

    `worstRating` (1) świadomie IGNORUJEMY. Rozciągnięcie [1,10] na [0,10]
    obniżyłoby każdą ocenę o ~0.5-1.0 pkt, a wakacje.pl publikuje swoje oceny
    w tej samej konwencji „x na 10" z jedynką jako dnem (w bazie są wartości
    1.8 i 4.6, nie ma zer). Obie liczby mają być porównywalne z tym, co
    użytkownik widzi na obu stronach — a nie z idealną skalą.
    """
    v = _as_float(value)
    if v is None:
        return None
    b = _as_float(best)
    if b is None:
        b = 10.0
    if b <= 0:                       # `bestRating: 0` to śmieć, nie skala
        return None
    return round(max(0.0, min(10.0, v / b * 10.0)), 2)


def parse_hotel_page(html: str, max_reviews: int = 5) -> ExternalRating:
    """Wyciąga ocenę ze strony hotelu HolidayCheck (blok JSON-LD schema.org).

    Wydzielone z pobierania, żeby dało się testować na zapisanej próbce.
    Hotel BEZ ani jednej opinii ma poprawny JSON-LD, ale bez `aggregateRating` —
    to normalny stan (`no_rating`), nie awaria.
    """
    out = ExternalRating(status=ST_NO_RATING)
    data = _first_hotel_ld(html)
    if data is None:
        out.status = ST_ERROR
        out.error = "brak bloku JSON-LD na stronie"
        return out

    out.matched_name = str(data.get("name") or "")
    out.url = str(data.get("url") or "")

    agg = data.get("aggregateRating") or {}
    if not isinstance(agg, dict) or agg.get("ratingValue") is None:
        return out                      # hotel jest, opinii nie ma

    out.rating = normalize_to_10(agg.get("ratingValue"), agg.get("bestRating", 10),
                                 agg.get("worstRating", 1))
    count = _as_float(agg.get("ratingCount"))
    out.review_count = int(count) if count is not None else None
    out.status = ST_OK if out.rating is not None else ST_NO_RATING

    for r in (data.get("review") or [])[:max_reviews]:
        if not isinstance(r, dict):
            continue
        txt = " ".join(x for x in [str(r.get("headline") or "").strip(),
                                   str(r.get("reviewBody") or "").strip()] if x)
        if txt:
            out.reviews.append(re.sub(r"\s+", " ", txt))
    return out


def _first_hotel_ld(html: str) -> dict | None:
    """Pierwszy blok JSON-LD opisujący hotel.

    Strona serwuje JSON-LD z zescapowanymi ukośnikami (`\\u002F`), a atrybut
    `type` bywa BEZ cudzysłowów (`<script type=application/ld+json>`) — stąd
    luźniejszy regex niż zwykle.
    """
    for raw in _LD_RE.findall(html or ""):
        try:
            data = json.loads(raw.strip().replace("\\u002F", "/"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), None)
        if isinstance(data, dict) and data.get("name"):
            return data
    return None


def parse_suggestions(payload: dict) -> list[dict]:
    """Kandydaci z odpowiedzi `suggestionSearch` -> [{id, name, place}]."""
    hotels = (((payload or {}).get("data") or {}).get("suggestionSearch") or {}).get("hotels")
    out = []
    for e in ((hotels or {}).get("entities") or []):
        if isinstance(e, dict) and e.get("id"):
            out.append({"id": str(e["id"]), "name": str(e.get("name") or ""),
                        "place": str(e.get("placeDetailString") or "")})
    return out


def pick_match(hotel_name: str, country: str, city: str, region: str,
               candidates: Iterable[dict]) -> tuple[dict | None, float, str]:
    """Wybiera najlepszego kandydata i orzeka o statusie dopasowania.

    Kraj jest filtrem twardym — kandydat z innego kraju nie jest kandydatem,
    nawet przy identycznej nazwie. Wśród pozostałych wygrywa najwyższe
    podobieństwo nazwy, a zgodność miasta/regionu służy jako rozstrzygacz
    remisów (stąd niewielka premia, która NIE trafia do zwracanej pewności).

    REGUŁA RYWALA. Jeśli w tej samej miejscowości jest DRUGI kandydat równie
    zgodny z naszą nazwą (`is_compatible`), orzekamy `ambiguous` — nawet gdy
    zwycięzca ma nazwę identyczną. Powód jest empiryczny: w Platamonas stoją
    obok siebie „Hotel Sun Beach" (8 opinii, 4.7) i „Sun Beach Platamon
    Resort", a w Ölüdeniz „Hotel Karbel", „Hotel Karbel Sun" i „Hotel Karbel
    Beach". Baza ofert ma tylko „Sun Beach (Platamonas)" i „Karbel" — nie ma
    czym rozstrzygnąć, o który obiekt chodzi.

    Warunek „równie dobrze ulokowany" jest istotny: „Alkyonides Boutique Hotel"
    w Kremasti na Rodos ma imiennika („Hotel Alcionides / Alkyonides") w Stalis
    na Krecie. Nazwy nie do odróżnienia, ale miejscowość owszem — więc to nie
    jest rywal i trafienie zostaje pewne.

    To kosztuje zasięg (kilka hoteli mniej z drugim źródłem), ale koszt pomyłki
    jest wyższy: podstawiona ocena obcego hotelu zapaliłaby fałszywą flagę
    „rozbieżność" i kazała odrzucić dobry hotel. „Brak danych" jest uczciwsze
    niż pewna odpowiedź na złe pytanie.

    Zwraca `(kandydat, pewność 0-1, status)`.
    """
    oceny: list[tuple[float, float, bool, dict]] = []   # (rank, sim, place_ok, cand)
    for cand in candidates:
        country_ok, place_ok = place_agreement(country, city, region, cand.get("place", ""))
        if not country_ok:
            continue
        sim = name_similarity(hotel_name, cand.get("name", ""))
        oceny.append((sim + (0.05 if place_ok else 0.0), sim, place_ok, cand))

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


# ------------------------------------------------------------------ wiarygodność

@dataclass
class Reliability:
    """Na ile wolno wierzyć ocenie hotelu, biorąc pod uwagę WSZYSTKIE źródła.

    Pola po `reason` doszły wraz z drugim źródłem zewnętrznym (Google) i mają
    wartości domyślne, żeby stary, pięcioargumentowy konstruktor dalej działał.
    """
    level: str                  # "niska" / "średnia" / "wysoka"
    diff: Optional[float]       # |wakacje.pl - zewnętrzne| SUROWE, None gdy brak
    divergent: bool             # rozjazd (po kalibracji, jeśli jest) > DIVERGENCE_PTS
    thin: bool                  # ocena wakacje.pl stoi na ≤3 opiniach
    reason: str
    sources: tuple[str, ...] = ()      # źródła, które realnie weszły do werdyktu
    diff_adj: Optional[float] = None   # rozjazd PO odjęciu systematyki źródła
    agreement: bool = False            # ≥2 źródła zewnętrzne zgodne między sobą


# ---------------------------------------------------------------- kalibracja

# Poniżej tylu par nie ma czego kalibrować — mediana z dwóch liczb to nie
# systematyka, tylko przypadek.
MIN_CALIBRATION_PAIRS = 3
# Korekta większa niż próg rozbieżności zamiotłaby problem pod dywan: gdyby
# źródło zaniżało medianowo o 3 pkt, „skalibrowanie" go sprawiłoby, że żaden
# hotel nigdy by już nie odstawał. Przycinamy — i mówimy o tym w raporcie.
MAX_CALIBRATION_PTS = DIVERGENCE_PTS


@dataclass
class SourceOffset:
    """Systematyka jednego źródła, policzona z BIEŻĄCEJ próbki.

    `median` to mediana różnicy `wakacje.pl − źródło`. Dodatnia znaczy, że
    źródło ocenia SUROWIEJ niż wakacje.pl (tak robi HolidayCheck), ujemna —
    że ŁAGODNIEJ (tak robi Google, bo ocenia go szeroka publiczność: także
    goście restauracji i baru, nie tylko nocujący).
    """
    source: str
    n: int
    median: float
    applied: float              # `median` przycięta do ±MAX_CALIBRATION_PTS
    enough: bool                # czy próbka wystarcza, by korektę stosować

    @property
    def label(self) -> str:
        kierunek = "surowiej" if self.median > 0 else "łagodniej"
        # „na N parach" zamiast „N par" — polska odmiana liczebnika zmienia się
        # przy 2-4 vs 5+, a ta forma jest poprawna dla każdego N.
        return (f"{self.source}: medianowo {-self.median:+.1f} pkt "
                f"({kierunek} niż wakacje.pl, na {self.n} parach)")


def calibrate(pairs: Iterable[tuple[Optional[float], "ExternalRating"]]
              ) -> dict[str, SourceOffset]:
    """Liczy systematykę KAŻDEGO źródła osobno z podanych par.

    Po co: HolidayCheck jest niemiecki i ocenia surowiej (poprzednik zmierzył
    medianowo −0,6 pkt względem wakacje.pl), a Google ocenia łagodniej, bo
    pyta wszystkich, nie tylko nocujących. Gdyby porównywać oceny wprost,
    narzędzie mierzyłoby RÓŻNICĘ KULTUR OCENIANIA, a nie jakość hotelu —
    i zapalałoby flagę „rozbieżność" na hotelach, z którymi wszystko w porządku.

    Dlatego korekta NIE jest wpisana na sztywno: liczy się ją z tego, co
    akurat jest w próbce, i pokazuje użytkownikowi (`SourceOffset.label`).
    Zaszyta stała byłaby niewidzialna i zestarzałaby się bez ostrzeżenia.

    Mediana, nie średnia: jeden hotel z rozjazdem 4 pkt (a takie są — to cała
    wartość tego narzędzia) przesunąłby średnią i skalibrował system tak,
    by przestał go widzieć.
    """
    zebrane: dict[str, list[float]] = {}
    for local, ext in pairs:
        if ext is None or not ext.usable or local is None or ext.rating is None:
            continue
        zebrane.setdefault(ext.source, []).append(local - ext.rating)

    out: dict[str, SourceOffset] = {}
    for src, roznice in zebrane.items():
        med = round(statistics.median(roznice), 2)
        out[src] = SourceOffset(
            source=src, n=len(roznice), median=med,
            applied=round(max(-MAX_CALIBRATION_PTS, min(MAX_CALIBRATION_PTS, med)), 2),
            enough=len(roznice) >= MIN_CALIBRATION_PAIRS,
        )
    return out


def offsets_map(cal: dict[str, SourceOffset]) -> dict[str, float]:
    """Same liczby korekt, tylko dla źródeł z wystarczającą próbką."""
    return {s: o.applied for s, o in (cal or {}).items() if o.enough and o.applied}


def _calibrated(ext: "ExternalRating", offsets: dict[str, float] | None) -> Optional[float]:
    """Ocena źródła przeniesiona na skalę wakacje.pl.

    `ocena + mediana(wakacje.pl − źródło)` — z definicji mediany reszta
    dla typowego hotelu wychodzi zero, więc to, co zostaje, jest realną
    różnicą zdań o TYM hotelu, a nie o kulturze oceniania.
    """
    if ext is None or ext.rating is None:
        return None
    return round(ext.rating + (offsets or {}).get(ext.source, 0.0), 2)


# ------------------------------------------------------------ wiarygodność

def reliability(local_rating: Optional[float], local_count: Optional[int],
                external: ExternalRating | None) -> Reliability:
    """Pewność oceny przy JEDNYM źródle zewnętrznym — bez kalibracji.

    Zachowana bez zmian w zachowaniu (i w treści komunikatów) dla wszystkiego,
    co powstało przed Google. Nowy kod woła `reliability_multi`.
    """
    return reliability_multi(local_rating, local_count,
                             [] if external is None else [external], None)


def reliability_multi(local_rating: Optional[float], local_count: Optional[int],
                      externals: Iterable["ExternalRating"],
                      offsets: dict[str, float] | None = None) -> Reliability:
    """Pewność oceny przy DOWOLNEJ liczbie źródeł zewnętrznych.

    Zasady, w kolejności ważności:

    1. **Źródła, które kłócą się MIĘDZY SOBĄ, znaczą „nie wiadomo".** Dwa
       niezależne serwisy z setkami opinii, które rozjeżdżają się o 2 pkt,
       nie dają średniej — dają brak rozstrzygnięcia. Pewność: niska.
    2. **Źródła zgodne między sobą, ale niezgodne z wakacje.pl, dają werdykt
       JEDNOZNACZNY.** To jest cały sens trzeciego źródła: „10.0 z jednej
       opinii" kontra 4.2/5 z 800 opinii Google to nie remis 1:1, tylko
       zdemaskowana ocena lokalna. Pewność niska + zapalona flaga rozbieżności.
    3. **Zgoda dwóch niezależnych źródeł podnosi pewność.** Przy ≥2 zgodnych
       źródłach do „wysokiej" wystarczy 20 opinii łącznie zamiast 30 —
       niezależność źródeł jest wartością samą w sobie, a nie tylko dopisaniem
       opinii do wspólnego worka.
    4. **Bez źródła zewnętrznego sufitem jest „średnia".** Jedno źródło nigdy
       nie daje pewności wysokiej — po to jest drugie.

    Rozjazdy liczone są PO KALIBRACJI (`offsets`), gdy jest czym kalibrować.
    Pole `diff` zostaje surowe (to, co użytkownik widzi na obu stronach),
    a `diff_adj` niesie resztę ponad systematykę — i to ona zapala flagę.
    """
    externals = [e for e in externals if e is not None]
    thin = (local_count or 0) <= THIN_EVIDENCE
    usable = [e for e in externals if e.usable]
    srodla = tuple(e.source for e in usable)

    # --- brak jakiegokolwiek użytecznego źródła --------------------------
    if not usable:
        powod = "brak drugiego źródła"
        if any(e.status == ST_AMBIGUOUS for e in externals):
            powod = "dopasowanie niepewne — pominięte"
        elif any(e.status == ST_NO_RATING for e in externals):
            powod = "hotel bez opinii w drugim źródle"
        if thin:
            return Reliability("niska", None, False, True,
                               f"{powod}; ocena z {local_count or 0} opinii")
        if (local_count or 0) >= 50:
            return Reliability("średnia", None, False, False,
                               f"{powod}; sporo opinii lokalnych")
        return Reliability("niska", None, False, False, powod)

    razem = (local_count or 0) + sum(e.review_count or 0 for e in usable)

    # --- dokładnie jedno źródło ------------------------------------------
    if len(usable) == 1:
        e = usable[0]
        diff = _diff(local_rating, e.rating)
        adj = _diff(local_rating, _calibrated(e, offsets))
        flaga = adj if adj is not None else diff

        if flaga is not None and flaga > DIVERGENCE_PTS:
            powod = f"źródła rozjeżdżają się o {diff} pkt"
            if adj is not None and adj != diff:
                powod += f" ({adj} pkt ponad systematykę źródła)"
            return Reliability("niska", diff, True, thin, powod, srodla, adj)
        if diff is None:
            return Reliability("niska", None, False, thin, "brak jednej z ocen",
                               srodla, adj)
        if razem >= 30 and flaga <= 1.0:
            return Reliability("wysoka", diff, False, thin,
                               f"zgodne oceny, łącznie {razem} opinii", srodla, adj)
        if razem >= 10:
            return Reliability("średnia", diff, False, thin,
                               f"zgodne oceny, łącznie {razem} opinii", srodla, adj)
        return Reliability("niska", diff, False, thin,
                           f"zgodne, ale łącznie tylko {razem} opinii", srodla, adj)

    # --- dwa źródła lub więcej -------------------------------------------
    skalibrowane = [_calibrated(e, offsets) for e in usable]
    wagi = [max(e.review_count or 0, 1) for e in usable]
    rozstrzal = round(max(skalibrowane) - min(skalibrowane), 2)
    zgodne = rozstrzal <= DIVERGENCE_PTS

    konsensus_raw = _wazona([e.rating for e in usable], wagi)
    konsensus_cal = _wazona(skalibrowane, wagi)
    diff = _diff(local_rating, konsensus_raw)
    adj = _diff(local_rating, konsensus_cal)
    opis = " i ".join(f"{e.source} {e.rating:.1f} ({e.review_count or 0})" for e in usable)

    if not zgodne:
        return Reliability(
            "niska", diff, True, thin,
            f"źródła zewnętrzne nie zgadzają się ze sobą ({rozstrzal} pkt): {opis}",
            srodla, adj, False)

    if adj is None:
        return Reliability("niska", diff, False, thin,
                           f"{len(usable)} zgodne źródła, brak oceny lokalnej",
                           srodla, None, True)

    if adj > DIVERGENCE_PTS:
        return Reliability(
            "niska", diff, True, thin,
            f"{len(usable)} niezależne źródła zgodne ({konsensus_cal:.1f} po kalibracji), "
            f"wakacje.pl odstaje o {adj} pkt: {opis}",
            srodla, adj, True)

    if razem >= 20:
        return Reliability("wysoka", diff, False, thin,
                           f"{len(usable)} niezależne źródła potwierdzają ocenę, "
                           f"łącznie {razem} opinii", srodla, adj, True)
    if razem >= 10:
        return Reliability("średnia", diff, False, thin,
                           f"{len(usable)} zgodne źródła, łącznie {razem} opinii",
                           srodla, adj, True)
    return Reliability("niska", diff, False, thin,
                       f"{len(usable)} zgodne źródła, ale łącznie tylko {razem} opinii",
                       srodla, adj, True)


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(abs(a - b), 2)


def _wazona(wartosci: list[Optional[float]], wagi: list[int]) -> Optional[float]:
    """Średnia ważona liczbą opinii.

    Google z 800 opiniami waży więcej niż HolidayCheck z czterema — i tak
    właśnie ma być. Waga minimalna to 1, żeby źródło z zerowym licznikiem
    nie zniknęło całkowicie.
    """
    pary = [(v, w) for v, w in zip(wartosci, wagi) if v is not None]
    if not pary:
        return None
    suma = sum(w for _, w in pary)
    return round(sum(v * w for v, w in pary) / suma, 2) if suma else None


# ------------------------------------------------------------------------ cache

def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


class ExternalRatingStore:
    """Cache PERMANENTNY, wzorowany na `ai/verdicts.py`.

    Hotel nie zmienia się z dnia na dzień, a jego ocena na HolidayCheck tym
    bardziej — przy 300 opiniach jedna nowa nie ruszy średniej na pierwszym
    miejscu po przecinku. Dlatego nie ma TTL-a: odświeżenie jest jawne
    (`--refresh`). Zapisujemy też porażki (`no_match`, `ambiguous`,
    `no_rating`), bo to trwałe fakty — hotel, którego nie ma na HolidayCheck,
    nie pojawi się tam do jutra, a ponawianie kosztowałoby po dwa żądania
    za każdym uruchomieniem.

    Wyjątkiem są statusy z `TRANSIENT_STATUSES`: `error` (sieć padła) i
    `no_key` (źródło czeka na klucz API) — `get` traktuje je jak brak wpisu,
    bo oba są stanami chwilowymi. Dzięki `no_key` Google rusza SAM w dniu,
    w którym użytkownik wgra klucz, bez czyszczenia cache'u.
    """

    def __init__(self, db: sqlite3.Connection | str | Path):
        if isinstance(db, sqlite3.Connection):
            self.db = db
        else:
            path = Path(db)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        ensure_schema(self.db)

    def get(self, hotel_id: str, source: str = SOURCE) -> ExternalRating | None:
        row = self.db.execute(
            "SELECT * FROM hotel_external_rating WHERE hotel_id=? AND source=?",
            (str(hotel_id), source),
        ).fetchone()
        if not row or row["status"] in TRANSIENT_STATUSES:
            return None
        return ExternalRating(
            hotel_id=str(row["hotel_id"]), source=row["source"],
            matched_name=row["matched_name"] or "", rating=row["rating_0_10"],
            review_count=row["review_count"], url=row["url"] or "",
            confidence=row["confidence"] or 0.0, status=row["status"],
            fetched_at=row["fetched_at"],
        )

    def put(self, rating: ExternalRating) -> None:
        self.db.execute(
            """INSERT INTO hotel_external_rating
               (hotel_id, source, matched_name, rating_0_10, review_count,
                url, confidence, status, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(hotel_id, source) DO UPDATE SET
                   matched_name=excluded.matched_name,
                   rating_0_10=excluded.rating_0_10,
                   review_count=excluded.review_count,
                   url=excluded.url,
                   confidence=excluded.confidence,
                   status=excluded.status,
                   fetched_at=excluded.fetched_at""",
            (str(rating.hotel_id), rating.source, rating.matched_name, rating.rating,
             rating.review_count, rating.url, rating.confidence, rating.status,
             rating.fetched_at or datetime.now().isoformat(timespec="seconds")),
        )
        self.db.commit()

    def count(self, source: str = SOURCE) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM hotel_external_rating WHERE source=?", (source,)
        ).fetchone()[0])


# --------------------------------------------------------------------- pobieranie

class HolidayCheckRatings:
    """Klient HolidayCheck. NIGDY nie rzuca przy błędzie sieci.

    Brak trafienia i padnięte źródło to dla wyszukiwarki ofert normalne stany
    („brak danych"), a nie awaria przebiegu — dokładnie jak w `ai/opinions.py`.
    Wszystko wraca jako `ExternalRating` ze statusem.
    """

    name = SOURCE

    def __init__(self, delay: float = 2.0, timeout: float = 30.0,
                 http: httpx.Client | None = None):
        self.delay = delay
        self._http = http
        self._timeout = timeout
        self._last_call = 0.0

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout, follow_redirects=True,
                                      headers=BROWSER_HEADERS)
        return self._http

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if self._last_call and gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    def suggest(self, query: str, limit: int = 8) -> tuple[list[dict], str]:
        """Kandydaci z `suggestionSearch` -> `(kandydaci, błąd)`.

        Pusta lista znaczy dwie zupełnie różne rzeczy: „tego hotelu tam nie ma"
        (fakt trwały, wart cache'owania) albo „sieć padła" (stan chwilowy, wart
        ponowienia). Gdyby jedno przebrało się za drugie, jedna awaria sieci
        zapisałaby cały ranking jako `no_match` na zawsze — dlatego powód
        wraca osobno.

        Endpoint bywa kapryśny i przy poprawnym zapytaniu potrafi oddać
        `Cannot read properties of undefined (reading 'body')`. To też jest
        błąd chwilowy, więc traktujemy go jak sieciowy.
        """
        self._throttle()
        try:
            r = self.http.post(
                GQL_URL,
                content=json.dumps({"query": SUGGEST_QUERY,
                                    "variables": {"q": query, "limit": limit}}),
                headers={"Accept": "*/*", "Content-Type": "application/json",
                         "Referer": "https://www.holidaycheck.de/hotelsuche"},
            )
            if r.status_code != 200:
                return [], f"HTTP {r.status_code}"
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return [], f"sieć: {exc}"
        if body.get("errors"):
            return [], f"GraphQL: {str(body['errors'])[:120]}"
        return parse_suggestions(body), ""

    def hotel_page(self, uuid: str) -> ExternalRating:
        """Strona hotelu po UUID. `/hi/x/<uuid>` przekierowuje na kanoniczny
        adres, więc sluga nie trzeba znać."""
        self._throttle()
        try:
            r = self.http.get(
                HOTEL_URL.format(uuid=uuid),
                headers={"Accept": "text/html,application/xhtml+xml",
                         "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
                         "sec-fetch-site": "none", "upgrade-insecure-requests": "1"},
            )
        except httpx.HTTPError as exc:
            return ExternalRating(status=ST_ERROR, error=f"sieć: {exc}")
        if r.status_code != 200:
            return ExternalRating(status=ST_ERROR, error=f"HTTP {r.status_code}")
        out = parse_hotel_page(r.text)
        if not out.url:
            out.url = str(r.url)
        return out

    def fetch(self, hotel_id: str, hotel_name: str, country: str = "",
              city: str = "", region: str = "") -> ExternalRating:
        """Pełna droga: nazwa -> kandydaci -> dopasowanie -> ocena.

        Do zapytania dokładamy miasto (a gdy go brak — region), bo samo
        „Olympia" trafia w tysiące obiektów, a „Olympia Pefkochori" w jeden.
        Składanie frazy jest jednak nietrywialne — patrz `search_query`.
        """
        now = datetime.now().isoformat(timespec="seconds")
        query = search_query(hotel_name, city, region)
        if not query:
            return ExternalRating(hotel_id=str(hotel_id), status=ST_NO_MATCH,
                                  fetched_at=now, error="pusta nazwa hotelu")

        cands, blad = self.suggest(query)
        if blad:
            # Awaria źródła NIE może zapisać się jako trwałe „nie ma takiego
            # hotelu" — inaczej jedno padnięcie sieci wyłączyłoby drugie
            # źródło dla całego rankingu na stałe.
            return ExternalRating(hotel_id=str(hotel_id), status=ST_ERROR,
                                  fetched_at=now, error=blad)
        if not cands:
            return ExternalRating(hotel_id=str(hotel_id), status=ST_NO_MATCH,
                                  fetched_at=now, error="brak kandydatów")

        best, conf, status = pick_match(hotel_name, country, city, region, cands)
        if best is None or status == ST_AMBIGUOUS:
            return ExternalRating(
                hotel_id=str(hotel_id), matched_name=(best or {}).get("name", ""),
                confidence=conf, status=(ST_NO_MATCH if best is None else ST_AMBIGUOUS),
                fetched_at=now,
            )

        out = self.hotel_page(best["id"])
        out.hotel_id = str(hotel_id)
        out.confidence = conf
        out.fetched_at = now
        if not out.matched_name:
            out.matched_name = best["name"]
        return out


def get_or_fetch(store: ExternalRatingStore, client, hotel_id: str,
                 hotel_name: str, country: str = "", city: str = "",
                 region: str = "", refresh: bool = False) -> ExternalRating:
    """Cache -> sieć -> cache. Działa z KAŻDYM klientem źródła.

    Źródło bierzemy z `client.name` (a nie z zaszytego „holidaycheck"), bo
    klucz główny cache'u to `(hotel_id, source)` — inaczej Google nadpisywałby
    HolidayCheck w tym samym wierszu. Klient bez atrybutu `name` jest
    traktowany jak HolidayCheck, żeby stary kod wołający tę funkcję z atrapą
    nadal działał.

    Błędy sieci NIE trafiają do cache'u jako trwałe, ale są zapisywane ze
    statusem `error` (a brak klucza jako `no_key`), które `get` i tak
    zignoruje — dzięki temu widać w bazie, że próba była, a mimo to ponowi
    się przy następnym uruchomieniu.
    """
    source = getattr(client, "name", SOURCE)
    if not refresh:
        hit = store.get(str(hotel_id), source)
        if hit is not None:
            return hit
    fresh = client.fetch(hotel_id, hotel_name, country, city, region)
    store.put(fresh)
    return fresh


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
