"""Wykrywanie zdarzeń cenowych na podstawie price_snapshot.

Zasady:

- PRICE_DROP: najnowsza cena oferty vs jej poprzedni snapshot, spadek >=
  ``drop_pct`` (domyślnie 5%). Gdy oferta ma >=5 snapshotów w ostatnich 30
  dniach, dodatkowo wymagamy, żeby nowa cena była poniżej 20. percentyla
  tego okna — to reguła rozkładowa, a nie "coś spadło o X%", bo Mac śpi
  i próbkowanie bywa dziurawe (nierówne odstępy między pomiarami psułyby
  prostą regułę "poprzedni punkt"). Przy <5 snapshotach nie ma z czego
  liczyć sensownego percentyla, więc wystarczy sam spadek procentowy —
  tryb last-minute, gdzie liczy się szybkość, nie pewność statystyczna.
- NEW_OFFER: first_seen oferty mieści się w ostatnich 24h.
- PRICE_RISE: symetrycznie do PRICE_DROP, ale wyłącznie jako materiał do
  raportu `hs diff` — nigdy nie trafia do powiadomień.
- PRICE_FLOOR: bieżąca cena jest ŚCIŚLE najniższa w całej historii oferty
  i historia ma co najmniej ``hotel_index.MIN_SAMPLES_FOR_CLAIM`` pomiarów.
  Liczy to `hotel_index` — ten sam wskaźnik, który napędza `hs indeks`,
  żeby alert i tabela nigdy nie mówiły dwóch różnych rzeczy. PRICE_FLOOR
  ZASTĘPUJE PRICE_DROP dla tej samej oferty (rekord historii jest mocniejszą
  informacją niż „spadło o X%", a dwa pingi o tym samym to spam).
- OFFER_VANISHED: oferta była w poprzednim przebiegu profilu, a w bieżącym
  jej nie ma. Sama w sobie jest informacją, nie alarmem — patrz `notifiable`.

Anti-spam: tabela notification_log(offer_key, event_type, sent_at),
tworzona przez ten moduł na tym samym połączeniu SQLite co Storage.
To samo zdarzenie (ta sama oferta + typ) wysyłamy najwyżej raz na
``cooldown_days``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import hotel_index

DROP_PCT_DEFAULT = 5.0
COOLDOWN_DAYS_DEFAULT = 3
NEW_OFFER_HOURS_DEFAULT = 24

PERCENTILE_WINDOW_DAYS = 30
PERCENTILE_MIN_SNAPSHOTS = 5
PERCENTILE_THRESHOLD = 0.20  # 20. percentyl

# Zniknięcie po spadku o tyle procent uznajemy za sygnał wyprzedaży miejsc.
VANISH_AFTER_DROP_PCT = 5.0

# Gdy z przebiegu na przebieg znika WIĘCEJ niż tyle ofert, to prawie na pewno
# nie wyprzedaż, tylko obcięte wyniki: inny `--limit`, timeout dostawcy, zmiana
# filtrów. W takiej sytuacji nie zgłaszamy żadnego zniknięcia — jedna cicha
# przerwa jest znacznie tańsza niż kilkadziesiąt fałszywych alarmów.
VANISH_MAX_SHARE = 0.5

NOTIFIABLE_EVENT_TYPES = ("PRICE_DROP", "NEW_OFFER", "PRICE_FLOOR", "OFFER_VANISHED")

NOTIFICATION_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_offer_event
    ON notification_log(offer_key, event_type, sent_at);
"""


@dataclass
class DealEvent:
    """Zdarzenie cenowe z pełnym kontekstem oferty — gotowe do wypisania
    w powiadomieniu albo w raporcie diff, bez ponownego odpytywania bazy."""
    event_type: str          # PRICE_DROP | PRICE_RISE | NEW_OFFER | PRICE_FLOOR | OFFER_VANISHED
    offer_key: str
    hotel_name: str
    region: str
    city: str
    price_old: Optional[int]
    price_new: int
    pct_change: Optional[float]   # ujemne = spadek, dodatnie = wzrost
    url: str
    # Gotowe zdanie po polsku z kontekstem, którego nie da się odczytać z
    # samych liczb (ile było pomiarów, poprzednie minimum, czemu oferta
    # zniknęła). Pole ma wartość domyślną, więc cały istniejący kod
    # konstruujący DealEvent działa bez zmian.
    note: str = ""

    @property
    def amount_change(self) -> Optional[int]:
        if self.price_old is None:
            return None
        return self.price_new - self.price_old

    @property
    def is_sellout_signal(self) -> bool:
        """Zniknięcie POPRZEDZONE spadkiem ceny — klasyczna sygnatura
        „ostatnie miejsca poszły po obniżce". Wyliczane z pól, które i tak
        mamy, żeby nie wprowadzać osobnego stanu, który mógłby się rozjechać
        z `pct_change`."""
        return (self.event_type == "OFFER_VANISHED"
                and self.pct_change is not None
                and self.pct_change <= -VANISH_AFTER_DROP_PCT)


@dataclass
class RunDiff:
    """Wynik porównania dwóch przebiegów tego samego profilu."""
    price_changes: list[DealEvent]
    new_offers: list[DealEvent]
    disappeared: list[dict]   # offer_key, hotel_name, region, city, price, url


def ensure_schema(db) -> None:
    """Tworzy notification_log, jeśli jeszcze nie istnieje. Wywoływać na
    połączeniu sqlite3 ze Storage (`store.db`) — moduł nie zakłada
    własnego pliku bazy."""
    db.executescript(NOTIFICATION_LOG_SCHEMA)
    db.commit()


def _offer_row(db, offer_key: str):
    return db.execute(
        "SELECT key, hotel_name, region, city, url, first_seen, last_seen "
        "FROM offer WHERE key=?",
        (offer_key,),
    ).fetchone()


def _last_two_snapshots(db, offer_key: str):
    """Dwa najnowsze snapshoty tej oferty (malejąco po id — id rośnie
    monotonicznie z czasem wstawiania, więc jest bezpieczniejszym kluczem
    sortowania niż `ts`, gdy dwa zapisy trafią w tej samej sekundzie)."""
    return db.execute(
        "SELECT price, ts FROM price_snapshot WHERE offer_key=? ORDER BY id DESC LIMIT 2",
        (offer_key,),
    ).fetchall()


def _window_prices(db, offer_key: str, days: int) -> list[float]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT price FROM price_snapshot WHERE offer_key=? AND ts>=? ORDER BY ts",
        (offer_key, since),
    ).fetchall()
    return [r["price"] for r in rows]


def _percentile(values: list[float], pct: float) -> float:
    """Percentyl liczony ręcznie (interpolacja liniowa), żeby zachowanie na
    małych próbkach (5-10 punktów) było proste i przewidywalne."""
    if not values:
        return float("inf")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def detect_price_events(db, offer_key: str,
                         drop_pct: float = DROP_PCT_DEFAULT) -> Optional[DealEvent]:
    """Porównuje dwa ostatnie snapshoty oferty. Zwraca DealEvent typu
    PRICE_DROP lub PRICE_RISE, albo None, gdy zmiana jest poniżej progu,
    reguła percentylowa jej nie potwierdza, albo oferta ma <2 snapshoty."""
    snaps = _last_two_snapshots(db, offer_key)
    if len(snaps) < 2:
        return None
    newest, previous = snaps[0]["price"], snaps[1]["price"]
    if previous <= 0:
        return None
    pct = (newest - previous) / previous * 100.0

    if pct <= -drop_pct:
        window = _window_prices(db, offer_key, PERCENTILE_WINDOW_DAYS)
        if len(window) >= PERCENTILE_MIN_SNAPSHOTS:
            threshold = _percentile(window, PERCENTILE_THRESHOLD)
            if newest > threshold:
                # Spadek jest, ale w kontekście ostatnich 30 dni cena wciąż
                # nie jest wyjątkowo niska — nie budzimy fałszywego alarmu.
                return None
        event_type = "PRICE_DROP"
    elif pct >= drop_pct:
        event_type = "PRICE_RISE"
    else:
        return None

    offer = _offer_row(db, offer_key)
    if offer is None:
        return None

    return DealEvent(
        event_type=event_type, offer_key=offer_key,
        hotel_name=offer["hotel_name"], region=offer["region"], city=offer["city"],
        price_old=previous, price_new=newest, pct_change=round(pct, 1),
        url=offer["url"],
    )


def detect_new_offers(db, hours: int = NEW_OFFER_HOURS_DEFAULT) -> list[DealEvent]:
    """Oferty, których first_seen mieści się w ostatnich `hours` godzinach."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT key, hotel_name, region, city, url FROM offer WHERE first_seen>=?",
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        latest = db.execute(
            "SELECT price FROM price_snapshot WHERE offer_key=? ORDER BY id DESC LIMIT 1",
            (r["key"],),
        ).fetchone()
        out.append(DealEvent(
            event_type="NEW_OFFER", offer_key=r["key"],
            hotel_name=r["hotel_name"], region=r["region"], city=r["city"],
            price_old=None, price_new=latest["price"] if latest else 0,
            pct_change=None, url=r["url"],
        ))
    return out


def _money(n: int | None) -> str:
    return f"{int(n or 0):,}".replace(",", " ")


def detect_price_floor(db, offer_key: str) -> Optional[DealEvent]:
    """PRICE_FLOOR: bieżąca cena oferty jest najniższa w całej jej historii.

    Próg „co najmniej 5 pomiarów" i samo orzeczenie o minimum pochodzą z
    `hotel_index` (`at_historic_low`), a nie z lokalnej reguły — jedno
    źródło prawdy dla alertu i dla tabeli `hs indeks`.

    Świadomie liczymy to na poziomie POJEDYNCZEJ oferty, mimo że indeks
    potrafi też pulę całego hotelu: hotel bywa sprzedawany w wariantach o
    różnym poziomie cen (5 vs 11 nocy, BB vs AI), więc „rekord hotelu"
    zapalałby się za każdym razem, gdy do wyników wpadnie tańszy wariant —
    a to nie jest obniżka. Kontekst hotelu dokładamy do `note` jako tło.
    """
    idx = hotel_index.offer_index(db, offer_key)
    if idx is None or not idx.at_historic_low:
        return None

    offer = _offer_row(db, offer_key)
    if offer is None:
        return None

    pct = None
    if idx.previous_min_ppn:
        pct = round((idx.current_ppn - idx.previous_min_ppn) / idx.previous_min_ppn * 100.0, 1)

    note = (f"najniżej z {idx.time_points} pomiarów tej oferty "
            f"(poprzednie minimum {_money(idx.previous_min_price)} zł/os)")
    hotel = hotel_index.index_for_offer(db, offer_key)
    if hotel is not None and hotel.variants > 1:
        note += (f"; cały hotel: {hotel.variants} warianty, mediana "
                 f"{hotel.median_ppn:.0f} zł/os/noc")

    return DealEvent(
        event_type="PRICE_FLOOR", offer_key=offer_key,
        hotel_name=offer["hotel_name"], region=offer["region"], city=offer["city"],
        price_old=idx.previous_min_price, price_new=idx.current_price,
        pct_change=pct, url=offer["url"], note=note,
    )


def detect_vanished_offers(db, profile: str,
                            max_share: float = VANISH_MAX_SHARE) -> list[DealEvent]:
    """OFFER_VANISHED: oferta była w poprzednim przebiegu profilu, w bieżącym
    już jej nie ma.

    Dwa niezależne warunki, celowo oba:

    - różnica zbiorów po `run_id` (kto był w starym przebiegu, a kogo nie ma
      w nowym),
    - `offer.last_seen` NIE nowsze niż start bieżącego przebiegu. To łapie
      przypadek, w którym ofertę zobaczył w międzyczasie inny profil albo
      `hs search` (którego snapshoty trafiają pod innym `run_id`) — skoro
      ktoś ją widział po starcie tego przebiegu, to nie zniknęła.
      Porównanie jest ostre (`>`), bo znaczniki mają rozdzielczość sekundy:
      dwa przebiegi tuż po sobie miałyby ten sam `ts` i warunek nieostry
      wyciszałby wtedy każde zniknięcie.

    `price_new` to ostatnia znana cena (ta, z którą oferta zniknęła), a
    `price_old` — cena tuż przed nią, żeby `pct_change` odpowiadało na
    pytanie „czy przed zniknięciem staniała".
    """
    runs = latest_two_runs(db, profile)
    if len(runs) < 2:
        return []
    old_run, new_run = runs[0], runs[1]

    old_prices = _prices_for_run(db, old_run["id"])
    new_prices = _prices_for_run(db, new_run["id"])
    if not old_prices or not new_prices:
        # Pusty przebieg to awaria pobierania, a nie wyprzedaż całego kraju.
        return []

    missing = [k for k in old_prices if k not in new_prices]
    if len(missing) > max_share * len(old_prices):
        return []

    events: list[DealEvent] = []
    for key in missing:
        offer = _offer_row(db, key)
        if offer is None:
            continue
        if (offer["last_seen"] or "") > new_run["ts"]:
            continue   # widziana po starcie tego przebiegu — nie zniknęła

        snaps = _last_two_snapshots(db, key)
        if not snaps:
            continue
        last_price = snaps[0]["price"]
        prev_price = snaps[1]["price"] if len(snaps) > 1 else None
        pct = None
        if prev_price and prev_price > 0:
            pct = round((last_price - prev_price) / prev_price * 100.0, 1)

        if pct is not None and pct <= -VANISH_AFTER_DROP_PCT:
            note = (f"zniknęła z wyników po spadku o {abs(pct):.1f}% "
                    f"({_money(prev_price)} → {_money(last_price)} zł/os) — "
                    f"tak wygląda wyprzedanie ostatnich miejsc po obniżce")
        else:
            note = (f"zniknęła z wyników przy cenie {_money(last_price)} zł/os "
                    f"(bez wcześniejszej obniżki — równie dobrze mogła tylko "
                    f"wypaść z pobieranego zakresu)")

        events.append(DealEvent(
            event_type="OFFER_VANISHED", offer_key=key,
            hotel_name=offer["hotel_name"], region=offer["region"], city=offer["city"],
            price_old=prev_price, price_new=last_price, pct_change=pct,
            url=offer["url"], note=note,
        ))
    return events


def scan_for_events(db, offer_keys: list[str] | None = None,
                     drop_pct: float = DROP_PCT_DEFAULT,
                     new_offer_hours: int = NEW_OFFER_HOURS_DEFAULT,
                     profile: str | None = None) -> list[DealEvent]:
    """Pełny skan: PRICE_DROP/PRICE_RISE/PRICE_FLOOR dla podanych ofert
    (domyślnie wszystkich w tabeli offer) + NEW_OFFER dla ostatnich
    `new_offer_hours` + OFFER_VANISHED, gdy podano `profile`.

    Oferty, które same są NEW_OFFER, mają tylko jeden snapshot, więc nie ma
    z czym ich porównać — pomijamy je w części cenowej.

    `profile` jest opcjonalny wyłącznie dla wstecznej zgodności: bez nazwy
    profilu nie da się powiedzieć, które przebiegi porównywać, więc
    detekcja zniknięć jest wtedy po prostu pomijana."""
    if offer_keys is None:
        offer_keys = [r["key"] for r in db.execute("SELECT key FROM offer").fetchall()]

    new_events = detect_new_offers(db, hours=new_offer_hours)
    new_keys = {e.offer_key for e in new_events}

    vanished = detect_vanished_offers(db, profile) if profile else []
    gone_keys = {e.offer_key for e in vanished}

    events: list[DealEvent] = list(new_events)
    for key in offer_keys:
        if key in new_keys:
            continue
        if key in gone_keys:
            # Oferty, której już nie ma w wynikach, nie zgłaszamy jako okazji —
            # „historyczne minimum, bierz" o czymś, czego nie da się kupić,
            # byłoby najgorszym możliwym powiadomieniem.
            continue
        floor = detect_price_floor(db, key)
        if floor is not None:
            # Rekord historii wchłania zwykły PRICE_DROP dla tej samej oferty:
            # zawsze towarzyszy mu spadek, a dwa powiadomienia o jednym
            # zdarzeniu to spam.
            events.append(floor)
            continue
        ev = detect_price_events(db, key, drop_pct=drop_pct)
        if ev:
            events.append(ev)

    events.extend(vanished)
    return events


def _in_cooldown(db, offer_key: str, event_type: str, cooldown_days: int) -> bool:
    since = (datetime.now() - timedelta(days=cooldown_days)).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT 1 FROM notification_log WHERE offer_key=? AND event_type=? AND sent_at>=? LIMIT 1",
        (offer_key, event_type, since),
    ).fetchone()
    return row is not None


def notifiable(db, events: list[DealEvent],
               cooldown_days: int = COOLDOWN_DAYS_DEFAULT) -> list[DealEvent]:
    """Filtruje zdarzenia do faktycznego wysłania.

    Przechodzą: PRICE_DROP, PRICE_FLOOR, NEW_OFFER oraz — pod warunkiem —
    OFFER_VANISHED. PRICE_RISE nigdy (to materiał wyłącznie do `hs diff`).

    OFFER_VANISHED jest domyślnie INFORMACJĄ, nie alarmem, i przechodzi tylko
    wtedy, gdy zniknięcie poprzedził spadek ceny (`is_sellout_signal`).
    Powód: przy godzinnym cyklu i twardym `--limit` oferty wpadają i
    wypadają z pobieranego zakresu bez przerwy — ranking dostawcy się
    przetasowuje, dostępność miga. Ping „oferta zniknęła" jest w dodatku
    z natury niewykonalny: skoro jej nie ma, nie ma czego kliknąć. Dopiero
    zniknięcie TUŻ PO obniżce niesie treść: potwierdza, że tamta obniżka
    była prawdziwa, a ten hotel wyprzedaje tanie miejsca szybko — czyli
    następnym razem trzeba reagować od razu. Pozostałe zniknięcia widać w
    `hs diff` i w podsumowaniu `hs monitor`.
    """
    ensure_schema(db)
    out = []
    for e in events:
        if e.event_type not in NOTIFIABLE_EVENT_TYPES:
            continue
        if e.event_type == "OFFER_VANISHED" and not e.is_sellout_signal:
            continue
        if _in_cooldown(db, e.offer_key, e.event_type, cooldown_days):
            continue
        out.append(e)
    return out


def mark_sent(db, event: DealEvent) -> None:
    """Odnotowuje wysyłkę — od tej chwili liczy się cooldown dla tej pary
    (offer_key, event_type)."""
    ensure_schema(db)
    db.execute(
        "INSERT INTO notification_log(offer_key, event_type, sent_at) VALUES (?,?,?)",
        (event.offer_key, event.event_type, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()


# ---------- diff między dwoma przebiegami (dla `hs diff`) ----------

def latest_two_runs(db, profile: str):
    """[starszy, nowszy] — dwa ostatnie przebiegi danego profilu, albo
    krótsza lista, gdy przebiegów jest mniej niż dwa."""
    rows = db.execute(
        "SELECT id, ts FROM run WHERE profile=? ORDER BY id DESC LIMIT 2", (profile,)
    ).fetchall()
    return list(reversed(rows))


def _prices_for_run(db, run_id: int) -> dict[str, int]:
    rows = db.execute(
        "SELECT offer_key, price FROM price_snapshot WHERE run_id=?", (run_id,)
    ).fetchall()
    return {r["offer_key"]: r["price"] for r in rows}


def diff_between_runs(db, profile: str, drop_pct: float = 0.0) -> Optional[RunDiff]:
    """Porównuje dwa ostatnie przebiegi profilu. `drop_pct` > 0 ogranicza
    listę zmian cen do tych >= progu (jak w detekcji okazji); domyślnie 0
    pokazuje KAŻDĄ faktyczną zmianę ceny — `hs diff` to raport do przeglądu
    przez człowieka, nie kanał powiadomień, więc nie warto nic ukrywać.
    Zwraca None, gdy profil ma mniej niż dwa przebiegi."""
    runs = latest_two_runs(db, profile)
    if len(runs) < 2:
        return None
    old_run_id, new_run_id = runs[0]["id"], runs[1]["id"]

    old_prices = _prices_for_run(db, old_run_id)
    new_prices = _prices_for_run(db, new_run_id)

    changes: list[DealEvent] = []
    new_offers: list[DealEvent] = []
    disappeared: list[dict] = []

    for key, new_price in new_prices.items():
        offer = _offer_row(db, key)
        if offer is None:
            continue
        if key not in old_prices:
            new_offers.append(DealEvent(
                event_type="NEW_OFFER", offer_key=key,
                hotel_name=offer["hotel_name"], region=offer["region"], city=offer["city"],
                price_old=None, price_new=new_price, pct_change=None, url=offer["url"],
            ))
            continue
        old_price = old_prices[key]
        if old_price <= 0 or old_price == new_price:
            continue
        pct = (new_price - old_price) / old_price * 100.0
        if drop_pct and abs(pct) < drop_pct:
            continue
        event_type = "PRICE_DROP" if pct < 0 else "PRICE_RISE"
        changes.append(DealEvent(
            event_type=event_type, offer_key=key,
            hotel_name=offer["hotel_name"], region=offer["region"], city=offer["city"],
            price_old=old_price, price_new=new_price, pct_change=round(pct, 1),
            url=offer["url"],
        ))

    for key, old_price in old_prices.items():
        if key in new_prices:
            continue
        offer = _offer_row(db, key)
        if offer is None:
            continue
        # Ten sam rozróżnik co w OFFER_VANISHED: „zniknęła po spadku" to
        # zupełnie inna wiadomość niż „zniknęła". Dokładane jako nowe klucze,
        # więc starsi konsumenci RunDiff.disappeared działają bez zmian.
        snaps = _last_two_snapshots(db, key)
        pct = None
        if len(snaps) > 1 and snaps[1]["price"] > 0:
            pct = round((snaps[0]["price"] - snaps[1]["price"]) / snaps[1]["price"] * 100.0, 1)
        disappeared.append({
            "offer_key": key, "hotel_name": offer["hotel_name"],
            "region": offer["region"], "city": offer["city"],
            "price": old_price, "url": offer["url"],
            "pct_change": pct,
            "after_drop": pct is not None and pct <= -VANISH_AFTER_DROP_PCT,
        })

    changes.sort(key=lambda e: e.pct_change or 0.0)
    return RunDiff(price_changes=changes, new_offers=new_offers, disappeared=disappeared)
