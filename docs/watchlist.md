# Watchlist konkretnych hoteli (`hs pilnuj`)

Naturalny tryb pracy, gdy masz już finalistów i czekasz na okazję: hotel
zostaje na radarze **niezależnie od bieżących filtrów profilu** — np. gdy
jego cena chwilowo wyskoczy ponad `max_price_pp`, i tak chcemy wiedzieć,
gdy spadnie z powrotem.

Moduł: `src/holiday_searcher/watchlist.py` (logika + baza) +
`src/holiday_searcher/cli_ext/watch.py` (komendy CLI). Wzorowane na module
`deals.py` / `hs diff` i `hs monitor`, ale działa na poziomie **hotelu**, nie
profilu.

## Komendy

```bash
hs pilnuj dodaj <hotel_id_lub_fragment_nazwy> [--cel CENA] [--profil NAZWA] [--notatka TEKST]
hs pilnuj lista
hs pilnuj usun <id_lub_fragment>
hs pilnuj sprawdz [--dry-run] [--delay 1.5] [--cooldown-days 2]
```

### `dodaj`

Dopasowuje hotel w bazie (`offer`) po dokładnym `hotel_id` albo po fragmencie
nazwy (`LIKE`, bez rozróżniania wielkości liter). Gdy pasuje więcej niż jeden
hotel, komenda **nie zgaduje** — wypisuje listę kandydatów (hotel_id, nazwa,
kraj, region) i prosi o doprecyzowanie (najlepiej przez `hotel_id`).

`--profil` ustala, z jakiego profilu (`config/profiles.yaml`) brać termin,
liczbę osób i lotnisko wylotu przy sprawdzaniu — **nie** filtry ceny/gwiazdek/
wyżywienia, te przy pilnowaniu celowo nie obowiązują. Domyślnie pierwszy
profil z pliku.

### `lista`

Tabela: hotel, kraj/region, cena docelowa, aktualna najtańsza cena z bazy
(z ostatnich zapisanych snapshotów), różnica do celu (na zielono, gdy już
poniżej celu), notatka, data dodania.

### `usun`

Dezaktywuje wpis (`active=0`) — **nie kasuje** historii ani wcześniej
wysłanych powiadomień. ID ma pierwszeństwo przed dopasowaniem po nazwie
(to, co pokazuje `lista`); gdy podany token nie jest liczbą albo nie pasuje
do żadnego ID, szuka po fragmencie nazwy/`hotel_id`. Wiele dopasowań —
podobnie jak w `dodaj` — wymaga doprecyzowania.

### `sprawdz`

Dla każdego aktywnego wpisu:

1. Pobiera z wakacje.pl aktualne warianty TEGO hotelu (`params.hotelId`),
   bez ograniczeń ceny/gwiazdek/wyżywienia z profilu.
2. Zapisuje je do bazy (`offer` + `price_snapshot`, append-only) — **zawsze**,
   też w `--dry-run`, tak samo jak `hs monitor` — to buduje historię, na
   podstawie której działa detekcja historycznego minimum.
3. Sprawdza trzy niezależne warunki alertu (patrz niżej) i wysyła
   powiadomienia Telegram przez odfiltrowaniu anti-spamu.

`--dry-run` wypisuje treść wiadomości na konsolę zamiast wysyłać i **nie
zużywa cooldownu** (drugie uruchomienie z `--dry-run` znowu pokaże to samo
zdarzenie).

## Warunki alertu

Każdy to osobny typ zdarzenia, osobno liczony do anti-spamu:

| Zdarzenie             | Warunek                                                                                     |
| ---------------------- | --------------------------------------------------------------------------------------------- |
| `WATCH_TARGET`        | najtańszy aktualny wariant hotelu spadł ≤ `target_price_pp`                                  |
| `WATCH_ATH`            | najtańszy aktualny wariant jest niżej niż **jakikolwiek** wcześniej zanotowany snapshot tego hotelu (`price_snapshot`). Pierwsze sprawdzenie nigdy tego nie odpala — nie ma z czym porównać. |
| `WATCH_NEW_CHEAPEST`   | pojawił się **nowy** wariant (inny termin/pokój/operator → nowy `offer.key`), tańszy niż dotychczasowy najtańszy AKTYWNY wariant tego hotelu                                                |

## Baza

```sql
CREATE TABLE watchlist (
    id, hotel_id, hotel_name, provider, profile,
    target_price_pp, note, added_at, active
);
CREATE TABLE watch_notification_log (id, watch_id, event_type, sent_at);
```

Tworzone idempotentnie (`CREATE TABLE IF NOT EXISTS`) przez
`watchlist.ensure_schema()` na tym samym połączeniu SQLite co `Storage`
(`data/offers.db`) — bez osobnego pliku bazy.

### Dlaczego własna tabela anti-spamu, nie `notification_log` z `deals.py`

`notification_log` (deals.py) jest kluczowana `offer_key` — czyli konkretnym
wariantem (termin + pokój + operator). Watchlista potrzebuje cooldownu na
poziomie **(hotel, typ zdarzenia)**: gdy pojawi się nowy termin tego samego
hotelu, to wciąż ten sam "temat" powiadomienia i nie powinien ominąć
cooldownu tylko dlatego, że `offer_key` jest inny. Stąd `watch_notification_log`
kluczowana `watch_id` (wpis watchlisty = hotel) + `event_type`.

## Pobieranie ofert jednego hotelu

Endpoint wakacje.pl (`POST /v2/api/offers`, metoda `search.tripsSearch`)
przyjmuje `params.hotelId: ["<id>"]` — payload budowany jest bezpośrednio
w `watchlist._hotel_payload()` (wzorowany na `_payload` w
`providers/wakacje.py`, ale bez `maxPrice`/`minCategory`/`service`, żeby
filtry profilu nie ograniczały widoczności hotelu). Sieć (retry, opóźnienie
≥ 1.5 s między zapytaniami) i mapowanie odpowiedzi na `Offer` są
**re-użyte**, nie duplikowane: `WakacjeProvider._post` / `WakacjeProvider._map`.
Plik `providers/wakacje.py` pozostaje nietknięty.

## Przykład

```bash
hs pilnuj dodaj 23141 --cel 1800 --notatka "TOP wybór, Alanya"
hs pilnuj dodaj "Sealine" --cel 2000
hs pilnuj lista
hs pilnuj sprawdz --dry-run
hs pilnuj usun 1
```
