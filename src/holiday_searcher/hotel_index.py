"""Indeks cen hotelu — porównanie hotelu Z SAMYM SOBĄ w czasie.

Scoring (`scoring.py`) odpowiada na pytanie „czy ten hotel jest tani na tle
rynku". Ten moduł odpowiada na inne, mocniejsze: „czy ten hotel jest tani
na tle SIEBIE SAMEGO". Zdanie „ten hotel chodził po 2 400–3 100 zł, teraz
jest 2 350" jest sygnałem okazji niezależnym od tego, jak wygląda koszyk
porównawczy — i odporne na jego znane obciążenie (patrz README, sekcja
„Znane ograniczenie").

Trzy decyzje, które łatwo zepsuć:

1. **Agregujemy po hotelu, nie po ofercie.** Warianty tego samego hotelu
   (inny termin, inne wyżywienie, inny pokój) to ta sama rzecz, a każdy z
   nich ma osobny `offer.key`. Historia liczona per klucz oferty byłaby
   pocięta na kawałki i przy godzinnym monitoringu prawie zawsze za krótka.

2. **Liczymy w zł/os/noc, nie w cenie pakietu.** W jednym hotelu potrafią
   współistnieć wyjazdy 5- i 11-nocne; minimum z surowych cen pakietu
   znaczyłoby wtedy tylko tyle, że ktoś kiedyś sprzedawał krótszy pobyt.
   `price_ppn` to jedyna wielkość, w której wolno porównywać (jak w
   `models.Offer.price_ppn` i w scoringu). Cenę pakietu pokazujemy obok,
   bo to w niej myśli człowiek.

3. **Przy krótkiej historii moduł milczy, zamiast zmyślać.** Każda pierwsza
   obserwacja jest jednocześnie minimum i maksimum — raportowanie jej jako
   okazji byłoby zwykłym kłamstwem statystycznym. Stąd pole `confidence` i
   próg `MIN_SAMPLES_FOR_CLAIM`: moduł liczy się nawet dla jednego
   snapshotu (degraduje się, nie wywala), ale sam mówi, czego z tych danych
   orzekać nie można. Uwaga: progiem jest liczba MOMENTÓW pomiaru
   (`time_points`), nie snapshotów — pięć wariantów hotelu pobranych w
   jednym przebiegu to pięć snapshotów i zero historii.

Klucz grupowania to (provider, hotel_id), a nie samo `hotel_id`. Numeracja
hoteli u wakacje.pl i r.pl jest niezależna, więc te same cyfry u dwóch
dostawców to dwa różne obiekty. Skutek uboczny: ten sam fizyczny hotel
widziany u obu dostawców daje dwa wiersze indeksu (sklejanie takich par to
zadanie `dedup.py`, świadomie tu niepowtarzane).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

# Poniżej tylu pomiarów nie orzekamy o „historycznym minimum" ani o pozycji
# percentylowej jako sygnale — jest to za mało, żeby cokolwiek znaczyło.
MIN_SAMPLES_FOR_CLAIM = 5

# „Dolne 20% własnej historii" — próg wyróżnienia w tabeli.
BOTTOM_QUANTILE = 0.20

# Progi etykiet pewności.
HIGH_CONF_SAMPLES = 10
HIGH_CONF_SPAN_DAYS = 3.0

CONF_NONE = "brak"
CONF_LOW = "niska"
CONF_MEDIUM = "średnia"
CONF_HIGH = "wysoka"

_CHUNK = 400  # bezpieczny rozmiar listy w SQL IN (...)


@dataclass(frozen=True)
class PriceSample:
    """Jeden punkt historii — snapshot ceny jednego wariantu hotelu.

    `seq` to `price_snapshot.id`: JEDYNY klucz porządkujący, jakiego tu
    używamy. `ts` bywa wsteczny (import, ręczna korekta) i ma rozdzielczość
    sekundy, więc sortowanie po nim potrafiłoby uznać za „bieżącą" cenę
    inny punkt niż detekcja zdarzeń w `deals`, która porządkuje po `id`.
    Dwa moduły muszą wskazywać ten sam ostatni pomiar."""
    seq: int
    ts: str
    price: int
    price_ppn: float
    offer_key: str

    @property
    def when(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.ts)
        except (TypeError, ValueError):
            return None


@dataclass
class HotelPriceIndex:
    """Pozycja bieżącej ceny na tle własnej historii hotelu (lub oferty).

    Wszystkie pola `*_ppn` są w zł za osobę za noc. `current_price` /
    `min_price` / `max_price` to ceny pakietu odpowiadające tym punktom —
    wyłącznie do pokazania człowiekowi, nie do porównań.
    """
    scope: str                 # "hotel" | "oferta"
    provider: str
    hotel_id: str
    hotel_name: str
    region: str
    city: str
    url: str

    current_offer_key: str
    current_price: int
    current_ppn: float
    current_nights: int
    current_ts: str

    min_ppn: float
    median_ppn: float
    max_ppn: float
    min_price: int
    max_price: int

    samples: int               # liczba snapshotów w historii
    time_points: int           # ile RÓŻNYCH momentów pomiaru (patrz `reliable`)
    variants: int              # ile różnych ofert (kluczy) złożyło się na historię
    span_days: float           # rozpiętość czasowa historii
    percentile: Optional[float]  # 0..1, pozycja bieżącej ceny; None przy 1 pomiarze
    confidence: str            # brak | niska | średnia | wysoka

    previous_min_ppn: Optional[float]   # minimum liczone BEZ bieżącego punktu
    previous_min_price: Optional[int]

    # ---- orzeczenia, na których wolno oprzeć alert ----

    @property
    def reliable(self) -> bool:
        """Czy historia jest na tyle długa, żeby w ogóle coś orzekać.

        Liczą się MOMENTY pomiaru, nie snapshoty. Hotel sprzedawany w pięciu
        wariantach daje pięć snapshotów w jednym przebiegu — i ani jednego
        punktu historii. Gdyby progiem było `samples`, taki hotel od razu po
        pierwszym pobraniu dostawałby etykietę „historyczne minimum",
        opisując w rzeczywistości tylko rozrzut cen między wariantami."""
        return self.time_points >= MIN_SAMPLES_FOR_CLAIM

    @property
    def is_strict_low(self) -> bool:
        """Bieżąca cena jest ściśle niższa niż KAŻDY wcześniejszy pomiar.

        Ściśle, nie „nie wyżej niż": cena płasko stojąca na minimum przez
        dziesięć przebiegów nie jest nowiną i nie ma jej po co zgłaszać.
        """
        return self.previous_min_ppn is not None and self.current_ppn < self.previous_min_ppn

    @property
    def at_historic_low(self) -> bool:
        """Jedyne pole, którym wolno uzasadnić komunikat „historyczne minimum"."""
        return self.reliable and self.is_strict_low

    @property
    def in_bottom_zone(self) -> bool:
        """Bieżąca cena w dolnych `BOTTOM_QUANTILE` własnej historii."""
        return (self.reliable and self.percentile is not None
                and self.percentile <= BOTTOM_QUANTILE)

    @property
    def vs_median_pct(self) -> Optional[float]:
        """Ile procent poniżej (ujemne) / powyżej własnej mediany jest teraz."""
        if not self.median_ppn:
            return None
        return round((self.current_ppn - self.median_ppn) / self.median_ppn * 100.0, 1)

    @property
    def spread_pct(self) -> Optional[float]:
        """Rozpiętość historii jako % minimum — 0 znaczy „cena nigdy nie drgnęła"."""
        if not self.min_ppn:
            return None
        return round((self.max_ppn - self.min_ppn) / self.min_ppn * 100.0, 1)

    def headline(self) -> str:
        """Jedno zdanie po polsku, uczciwe wobec długości historii."""
        if self.time_points < 2:
            if self.variants > 1:
                return (f"jeden moment pomiaru, {self.variants} warianty "
                        f"({self.min_ppn:.0f}–{self.max_ppn:.0f} zł/os/noc) — "
                        f"to rozrzut wariantów, jeszcze nie historia")
            return "pierwszy pomiar — brak historii do porównania"
        if not self.reliable:
            return (f"tylko {self.time_points} momenty/ów pomiaru — za mało, żeby "
                    f"orzekać o minimum (zakres {self.min_ppn:.0f}–{self.max_ppn:.0f} "
                    f"zł/os/noc)")
        if self.is_strict_low:
            return (f"najniżej w historii tego hotelu ({self.time_points} pomiarów, "
                    f"poprzednie minimum {self.previous_min_ppn:.0f} zł/os/noc)")
        if self.in_bottom_zone:
            return (f"w dolnych {int(BOTTOM_QUANTILE * 100)}% własnej historii "
                    f"({self.time_points} pomiarów)")
        return (f"typowa cena dla tego hotelu (mediana {self.median_ppn:.0f} zł/os/noc "
                f"z {self.time_points} pomiarów)")


# ---------------------------------------------------------------- obliczenia

def _percentile_position(values: list[float], current: float) -> Optional[float]:
    """Pozycja `current` w rozkładzie `values` (current jest jego częścią).

    Remisy liczone w połowie (midrank), więc płaska historia daje 0.5, a
    jedyne minimum przy n=10 daje 0.05 — a nie mylące zero.
    """
    n = len(values)
    if n < 2:
        return None
    below = sum(1 for v in values if v < current - 1e-9)
    equal = sum(1 for v in values if abs(v - current) <= 1e-9)
    return round((below + 0.5 * equal) / n, 4)


def _confidence(time_points: int, span_days: float) -> str:
    """Pewność liczona z liczby MOMENTÓW pomiaru (a nie snapshotów) i z tego,
    jak długi odcinek czasu obejmują — patrz `HotelPriceIndex.reliable`."""
    if time_points < 2:
        return CONF_NONE
    if time_points < MIN_SAMPLES_FOR_CLAIM:
        return CONF_LOW
    if time_points >= HIGH_CONF_SAMPLES and span_days >= HIGH_CONF_SPAN_DAYS:
        return CONF_HIGH
    return CONF_MEDIUM


def _span_days(samples: list[PriceSample]) -> float:
    stamps = [s.when for s in samples]
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        return 0.0
    # Zaokrąglenie do 2 miejsc gubiłoby odstępy krótsze niż ~15 minut i
    # pokazywało „brak rozpiętości" tam, gdzie są dwa realne pomiary.
    return round((max(stamps) - min(stamps)).total_seconds() / 86400.0, 5)


# ------------------------------------------------------------------ dostęp do bazy

def _chunks(seq: list, size: int = _CHUNK) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _offer_rows(db, keys: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in _chunks(keys):
        q = ",".join("?" * len(part))
        rows = db.execute(
            f"""SELECT key, provider, hotel_id, hotel_name, region, city, url, nights
                FROM offer WHERE key IN ({q})""", part).fetchall()
        for r in rows:
            out[r["key"]] = dict(r)
    return out


def _samples_for_keys(db, keys: list[str]) -> list[PriceSample]:
    """Historia posortowana po `id`, nie po `ts` (patrz `PriceSample.seq`)."""
    out: list[PriceSample] = []
    for part in _chunks(keys):
        q = ",".join("?" * len(part))
        rows = db.execute(
            f"""SELECT id, offer_key, ts, price, price_ppn FROM price_snapshot
                WHERE offer_key IN ({q}) ORDER BY id""", part).fetchall()
        out.extend(PriceSample(seq=int(r["id"]), ts=r["ts"], price=int(r["price"]),
                               price_ppn=round(float(r["price_ppn"]), 2),
                               offer_key=r["offer_key"]) for r in rows)
    out.sort(key=lambda s: s.seq)   # scalenie chunków i wariantów w jedną oś
    return out


def hotel_group_key(offer_row) -> tuple[str, str]:
    """(provider, hotel_id) — a gdy dostawca nie podał hotel_id, oferta jest
    swoim własnym „hotelem" (fallback po kluczu, nigdy po nazwie: dwa różne
    hotele potrafią nazywać się tak samo).

    Działa i na `sqlite3.Row`, i na zwykłym dict — oba indeksuje się `[]`."""
    hid = str(offer_row["hotel_id"] or "").strip()
    provider = str(offer_row["provider"] or "")
    return (provider, hid or f"key:{offer_row['key']}")


def keys_for_hotel(db, provider: str, hotel_id: str) -> list[str]:
    """Wszystkie klucze ofert tego samego hotelu u tego samego dostawcy."""
    rows = db.execute(
        "SELECT key FROM offer WHERE provider=? AND hotel_id=?", (provider, hotel_id)
    ).fetchall()
    return [r["key"] for r in rows]


def keys_for_profile(db, profile: str) -> list[str]:
    """Oferty, które wystąpiły w jakimkolwiek przebiegu tego profilu.

    Gdy snapshoty nie mają `run_id` (starsze dane albo zapisy spoza CLI),
    wynik jest pusty — wtedy wołający powinien wziąć całą bazę. Zwracamy
    pustą listę zamiast rzucać, bo to normalny stan, nie błąd."""
    rows = db.execute(
        """SELECT DISTINCT s.offer_key AS k FROM price_snapshot s
           JOIN run r ON r.id = s.run_id WHERE r.profile = ?""", (profile,)).fetchall()
    return [r["k"] for r in rows]


# --------------------------------------------------------------- budowanie indeksu

def _build(scope: str, offers: dict[str, dict], samples: list[PriceSample]) -> Optional[HotelPriceIndex]:
    if not samples or not offers:
        return None

    latest = samples[-1]
    head = offers.get(latest.offer_key) or next(iter(offers.values()))

    ppns = [s.price_ppn for s in samples]
    previous = ppns[:-1]
    prev_min = min(previous) if previous else None
    prev_min_price = None
    if prev_min is not None:
        prev_min_price = min(s.price for s in samples[:-1] if abs(s.price_ppn - prev_min) <= 1e-9)

    lo = min(samples, key=lambda s: s.price_ppn)
    hi = max(samples, key=lambda s: s.price_ppn)
    span = _span_days(samples)
    # Kilka wariantów zapisanych w jednym przebiegu ma ten sam `ts` — i słusznie
    # liczy się jako JEDEN moment pomiaru (patrz HotelPriceIndex.reliable).
    time_points = len({s.ts for s in samples})

    return HotelPriceIndex(
        scope=scope,
        provider=head.get("provider") or "",
        hotel_id=str(head.get("hotel_id") or ""),
        hotel_name=head.get("hotel_name") or "",
        region=head.get("region") or "",
        city=head.get("city") or "",
        url=head.get("url") or "",
        current_offer_key=latest.offer_key,
        current_price=latest.price,
        current_ppn=latest.price_ppn,
        current_nights=int(head.get("nights") or 0),
        current_ts=latest.ts,
        min_ppn=lo.price_ppn,
        median_ppn=round(statistics.median(ppns), 2),
        max_ppn=hi.price_ppn,
        min_price=lo.price,
        max_price=hi.price,
        samples=len(samples),
        time_points=time_points,
        variants=len({s.offer_key for s in samples}),
        span_days=span,
        percentile=_percentile_position(ppns, latest.price_ppn),
        confidence=_confidence(time_points, span),
        previous_min_ppn=prev_min,
        previous_min_price=prev_min_price,
    )


def offer_index(db, offer_key: str) -> Optional[HotelPriceIndex]:
    """Indeks liczony z historii JEDNEJ oferty (jednego `offer.key`)."""
    offers = _offer_rows(db, [offer_key])
    return _build("oferta", offers, _samples_for_keys(db, [offer_key]))


def hotel_index(db, provider: str, hotel_id: str) -> Optional[HotelPriceIndex]:
    """Indeks liczony z historii wszystkich wariantów danego hotelu."""
    keys = keys_for_hotel(db, provider, hotel_id)
    if not keys:
        return None
    return _build("hotel", _offer_rows(db, keys), _samples_for_keys(db, keys))


def index_for_offer(db, offer_key: str) -> Optional[HotelPriceIndex]:
    """Indeks HOTELA, do którego należy dana oferta — wygodne wejście dla
    detekcji zdarzeń, która operuje kluczami ofert. Gdy dostawca nie podał
    `hotel_id`, degraduje się do indeksu samej oferty."""
    row = db.execute(
        "SELECT key, provider, hotel_id FROM offer WHERE key=?", (offer_key,)).fetchone()
    if row is None:
        return None
    provider, hid = hotel_group_key(row)
    if hid.startswith("key:"):
        return offer_index(db, offer_key)
    return hotel_index(db, provider, hid)


def build_all(db, profile: str | None = None,
              offer_keys: list[str] | None = None) -> list[HotelPriceIndex]:
    """Indeksy wszystkich hoteli — domyślnie z całej bazy, opcjonalnie
    zawężone do profilu albo do konkretnych ofert.

    Sortowanie: najpierw hotele z wiarygodną historią, w kolejności pozycji
    percentylowej (czyli okazje na górze), potem reszta od najtańszej.
    Hotel bez historii nie może wyprzedzić hotelu, o którym coś wiemy —
    to ta sama zasada, co `UNRATED_CAP` w scoringu."""
    if offer_keys is None and profile:
        offer_keys = keys_for_profile(db, profile) or None
    if offer_keys is None:
        offer_keys = [r["key"] for r in db.execute("SELECT key FROM offer").fetchall()]
    if not offer_keys:
        return []

    offers = _offer_rows(db, list(offer_keys))

    # Zawężenie do profilu daje klucze wariantów; historię hotelu trzeba
    # jednak liczyć z WSZYSTKICH jego wariantów, także tych spoza zawężenia.
    groups: dict[tuple[str, str], set[str]] = {}
    for row in offers.values():
        groups.setdefault(hotel_group_key(row), set()).add(row["key"])
    for gk in list(groups):
        provider, hid = gk
        if not hid.startswith("key:"):
            groups[gk].update(keys_for_hotel(db, provider, hid))

    all_keys = sorted({k for ks in groups.values() for k in ks})
    full_offers = _offer_rows(db, all_keys)
    by_key: dict[str, list[PriceSample]] = {}
    for s in _samples_for_keys(db, all_keys):
        by_key.setdefault(s.offer_key, []).append(s)

    out: list[HotelPriceIndex] = []
    for gk, keys in groups.items():
        samples = [s for k in keys for s in by_key.get(k, [])]
        samples.sort(key=lambda s: s.seq)
        idx = _build("hotel" if not gk[1].startswith("key:") else "oferta",
                     {k: full_offers[k] for k in keys if k in full_offers}, samples)
        if idx:
            out.append(idx)

    out.sort(key=lambda i: (0 if i.reliable else 1,
                            i.percentile if i.percentile is not None else 1.0,
                            i.current_ppn))
    return out
