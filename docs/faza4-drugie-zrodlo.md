# Faza 4 — drugie źródło ofert (r.pl) i deduplikacja hoteli

## Wybór źródła

Kolejność prób z planu to r.pl → itaka.pl → travelplanet.pl → fly.pl.
**Zatrzymaliśmy się na pierwszym: r.pl (Rainbow Tours).** Spełnia wszystkie warunki:
czysty JSON bez przeglądarki, bez logowania, bez tokenów, bez Cloudflare.
Pozostałych trzech nie badano — nie było po co.

Dlaczego akurat to źródło jest sensownym uzupełnieniem wakacje.pl: wakacje.pl agreguje
kilkunastu organizatorów (Anex, Itaka, Coral, Join UP…), a r.pl to sklep własny
jednego z nich. Porównanie „ten sam hotel u agregatora vs u organizatora" jest więc
odpowiedzią na realne pytanie, a nie porównaniem dwóch kopii tego samego katalogu.

## Jak znaleziono endpoint

1. `GET https://r.pl/turcja` — Nuxt 3, HTTP 200, brak Cloudflare. Oferty są w SSR-owym
   payloadzie `__NUXT_DATA__` (format devalue — spłaszczona tablica z referencjami).
2. W payloadzie widać klucze `initGetOferty`, `initSzukajSortowanie` oraz store `filtry`
   ze słownikami lokalizacji, lotnisk i filtrów bocznych. Stamtąd pochodzą wszystkie
   słowniki wpisane do adaptera (slug kraju, regiony, kody IATA, filtry wyżywienia).
3. Bundle: 7 chunków z `https://dist.r.pl/core/1.1387658/_nuxt3-core/`. W nich mapa
   hostów `s9` i ścieżek API. Baza produkcyjna to `https://r.pl`.
4. `grep` po `$fetch(` wskazał dwie ścieżki: `WYSZUKIWARKA + "/wyszukaj"` oraz
   `BLOCZKI + "/pobierz-bloczki"`, gdzie `WYSZUKIWARKA = /api/wyszukiwarka/v5.0`,
   `BLOCZKI = /api/bloczki/v5.0` (stała `si = "v5.0"`).
5. Konstruktory ciała żądania (`gd`, `bd`, `bu` w chunku `CMASWDL-.js`) dały komplet
   nazw pól: `Sortowanie`, `Strona`, `Limit`, `Atrybuty`, `AtrybutyWyklucz`,
   `DatyUrodzenia`, `LiczbaPokoi`.

Ślepy trop: `/api/czartery/wyszukiwanie/v4.1` wygląda na endpoint wyszukiwania, ale
zwraca 301 na wewnętrzny host `rpl-backend-czartery.c3.rainbowtours.pl` i 404 —
to nie jest ścieżka używana przez frontend.

## Endpointy

| | |
|---|---|
| Wyszukiwanie | `POST https://r.pl/api/wyszukiwarka/v5.0/wyszukaj` |
| Opisy hoteli | `POST https://r.pl/api/bloczki/v5.0/pobierz-bloczki` |
| Auth | brak — bez ciasteczek, bez tokenów |
| Cloudflare | nie napotkano |
| Stronicowanie | `Strona` (od 1) + `Limit` (30 działa; 40 też) |
| Słowniki | `__NUXT_DATA__` na stronie kierunku (`https://r.pl/turcja`) |

Jedna strona wyników = **dwa zapytania**. `wyszukaj` zwraca same identyfikatory i ceny,
bez nazw hoteli:

```json
{"Count": 88, "CzyCenaZaOsobe": true, "KluczWyszukania": "…",
 "Wynik": [{"Id": "7451_12172:234589:9926647", "KluczGrupy": "7451_TRM",
            "Cena": 2680, "CenaBezPromocji": 2680, "LiczbaDni": 8,
            "TerminWyjazdu": "2026-08-31T00:00:00Z", "UnikalnyKluczOferty": "…"}]}
```

`pobierz-bloczki` dostaje **surowe elementy `Wynik`** w polu `Parametry` i dokłada opis:
`BazoweInformacje` (nazwa, `HotelId`, gwiazdki, `Panstwa`, `Regiony`, `LiczbaNocy`,
`OfertaUrl`), `Wyzywienia`, `Cena`, `Ocena`, `Przystanki`.

### Payload wyszukiwania

```json
{
  "Sortowanie": "cena-asc",
  "CzyWeekendowka": false,
  "PowrotNaInneLotnisko": false,
  "Strona": 1,
  "Limit": 30,
  "Atrybuty": {
    "Lokalizacje_HoteloProdukt": ["europa:turcja"],
    "Miasta": [],
    "TypTransportu": ["AIR", "DREAMLINER"],
    "TerminWyjazdu": ["2026-08-31", "2026-09-11"],
    "DlugoscPobytu": ["8-9"],
    "Wyzywienia": ["all-inclusive"],
    "StandardHotelu": ["8", "10"],
    "Cena": ["avg", "*-*"]
  },
  "AtrybutyWyklucz": {},
  "DatyUrodzenia": ["1990-01-01", "1990-01-01"],
  "LiczbaPokoi": 1
}
```

### Payload opisów

```json
{"Parametry": [ …elementy Wynik… ], "CzyCenaZaOsobe": true,
 "CzyZmienicZdjecia": false, "DatyUrodzenia": ["1990-01-01","1990-01-01"],
 "LiczbaPokoi": 1, "Route": "/"}
```

## Test ceny (obowiązkowy) — wynik

To samo zapytanie (Turcja, AI, 4–5★, 8 dni, wylot 31.08–11.09, sort `cena-asc`),
zmieniana wyłącznie liczba dorosłych. Porównywane te same hotele (klucz:
`KluczGrupy | termin | liczba dni`).

**Bez atrybutu `Cena` — cena jest ZA CAŁĄ GRUPĘ** (`CzyCenaZaOsobe: false`):

| hotel | 2 os. | 3 os. | 4 os. | 4/2 |
|---|---|---|---|---|
| 2148_TRI \| 2026-09-03 \| 8 | 8 018 | 11 296 | 14 895 | **1,86×** |
| 3123_TRM \| 2026-09-04 \| 8 | 7 501 | 10 675 | 14 425 | **1,92×** |
| 3152_TRI \| 2026-09-03 \| 8 | 8 846 | 12 492 | 18 187 | **2,06×** |
| 691_TRI \| 2026-09-03 \| 8 | 8 375 | 11 783 | 16 750 | **2,00×** |
| 7521_TRA \| 2026-09-02 \| 8 | 7 791 | 10 989 | 15 582 | **2,00×** |

**Z atrybutem `Cena: ["avg","*-*"]` — cena jest ZA OSOBĘ** (`CzyCenaZaOsobe: true`):

| hotel | 2 os. | 3 os. | 4 os. | 4/2 |
|---|---|---|---|---|
| 2148_TRI \| 2026-09-03 \| 8 | 4 009 | 3 766 | 3 724 | **0,93×** |
| 3123_TRM \| 2026-09-04 \| 8 | 3 751 | 3 559 | 3 607 | **0,96×** |
| 3152_TRI \| 2026-09-03 \| 8 | 4 423 | 4 164 | 4 547 | **1,03×** |
| 691_TRI \| 2026-09-03 \| 8 | 4 188 | 3 928 | 4 188 | **1,00×** |
| 7521_TRA \| 2026-09-02 \| 8 | 3 896 | 3 663 | 3 896 | **1,00×** |

Wniosek: przy 4 osobach cena bez `avg` rośnie dwukrotnie — to cena całkowita.
Z `avg` zostaje w tym samym rzędzie (spada dla 3–4 os., bo dostawki są tańsze) —
dokładnie tak, jak zachowuje się wakacje.pl. **Adapter zawsze wysyła `Cena: ["avg", …]`
i dodatkowo sprawdza flagę `CzyCenaZaOsobe` w każdym bloczku; gdyby wróciła jako
`false`, dzieli cenę przez liczbę osób.**

Potwierdzenie niezależne: w `hs compare` hotel **Asrin Beach** ma na ten sam termin
3 177 zł/os u obu dostawców — co do złotówki. Gdyby normalizacja była błędna,
r.pl wychodziłby dwa razy droższy.

## Pułapki i ograniczenia r.pl

- **`TerminWyjazdu` to `[wyjazd od, powrót do]`, nie zakres samego wyjazdu.** Okno
  węższe niż długość wycieczki daje `Count: 0` (np. `["2026-09-05","2026-09-06"]`
  przy pobycie 8-dniowym zwraca pustkę). To ta sama semantyka co
  `departureDate`/`arrivalDate` u wakacje.pl.
- **`DlugoscPobytu` liczy DNI, nie noce** (8 dni = 7 nocy). Filtr jest jednowartościowy;
  własne zakresy (`"8-9"`) są przyjmowane, ale nie zweryfikowaliśmy, czy backend ich
  nie normalizuje do koszyków `0-6 / 7-9 / 10-13 / 14-17`. Adapter dla bezpieczeństwa
  **dotnie liczbę nocy po swojej stronie**.
- **r.pl nie rozróżnia wariantów all inclusive.** Ma pięć koszyków: `all-inclusive`,
  `3-posilki`, `2-posilki`, `sniadania`, `bez-wyzywienia`. UAI / AI Plus / AI Soft
  nie istnieją — wszystko wpada do `AI`. Scoring premiujący UAI będzie więc systematycznie
  faworyzował wakacje.pl; przy porównaniu cen zestawiamy rodziny wyżywienia, nie kody.
- **Ocena klientów jest w skali szkolnej 1–6**, nie 0–10 jak u wakacje.pl (potwierdza to
  filtr „od 5.0" = wartość `10` z zakresu `0-12`). Adapter przelicza ×10/6, bo
  `scoring.py` dzieli ocenę przez 10.
- **Jedna oferta bywa dostępna z kilkunastu lotnisk w tej samej cenie** (`Przystanki`).
  Gdy profil nie wskazuje lotniska, adapter nie zmyśla jednego: wpisuje
  `„dowolne (N lotnisk)"` i kod `*`.
- **Bloczek nie podaje typu pokoju** — `room_type` zostaje puste.
- `TerminWyjazdu` w bloczku to godzina wylotu w UTC (np. `2026-08-31T21:00:00Z`);
  datę bierzemy z wyniku wyszukiwania (północ UTC), żeby uniknąć przesunięcia o dobę.
- Organizator jest zawsze ten sam — adapter wpisuje `Rainbow` na sztywno.

## Grzeczność

User-Agent przeglądarkowy, `delay >= 1.5 s` między zapytaniami (domyślnie 1.5 s,
sterowane `--delay`). Rekonesans zmieścił się w ok. 35 zapytaniach rozłożonych
w czasie, w tym 7 pobrań statycznych bundli z CDN.

## Deduplikacja hoteli (`dedup.py`)

Kanonizacja nazwy: lowercase → usunięcie diakrytyków (`ł`, `ı` osobno, reszta przez
NFKD) → wycięcie gwiazdek (`4*`, `*****`, `★`) → wycięcie interpunkcji → wycięcie słów
nieodróżniających (`hotel`, `resort`, `spa`, `club`, `&`, `the`, `by`, `all inclusive`…).
Jeśli po wycięciu nie zostaje nic (hotel nazywa się „Hotel Spa"), wracamy do wersji bez
wycinania słów — inaczej wszystkie takie obiekty byłyby tożsame.

Porównanie: `difflib.SequenceMatcher(...).ratio()` na postaciach kanonicznych.

| ratio | status | znaczenie |
|---|---|---|
| ≥ 0.85 | `auto` | ten sam hotel |
| 0.60 – 0.85 | `ambiguous` | kandydat — **nie rozstrzygamy sami**, etykieta zostaje w danych dla przyszłej oceny AI |
| < 0.60 | — | para w ogóle nie powstaje |

Kraj musi się zgadzać zawsze. Region — niekoniecznie, bo nazewnictwo się rozjeżdża:
wakacje.pl mówi „Wybrzeże Egejskie" tam, gdzie r.pl mówi „Marmaris" albo
„Riwiera Egejska". Dopasowanie po samym kraju jest dozwolone, ale z karą
`REGION_PENALTY = 0.10` do pewności — przez co słabsze pary same spadają do `ambiguous`.
Dopasowanie jest zachłanne i **jeden-do-jednego**: hotel może wejść tylko w jedną parę.

Zapis: tabela `hotel_alias(canonical_id, provider, provider_hotel_id, hotel_name,
canonical_name, country, region, confidence, status, updated_at)`, tworzona przez
`executescript` w samym module (nie zależy od `storage.py`). Obie strony pary dostają
ten sam `canonical_id` — sha1 z kraju i posortowanych par `provider:hotel_id`,
więc jest stabilny i niezależny od kolejności argumentów.

## `hs compare`

```bash
PYTHONPATH=src python3 -m holiday_searcher.cli compare turcja-wrzesien --limit 200
```

Pobiera oferty z obu źródeł, dedupikuje hotele i zestawia najbliższe sobie oferty:
ta sama rodzina wyżywienia, różnica długości pobytu ≤ 1 noc, spośród takich —
najbliższe terminem. Komenda jest **read-only wobec `offers.db`**: nie zapisuje ofert
ani snapshotów cen. Jedyne, co utrwala, to `hotel_alias`.

**Wielkość próbki ma znaczenie.** Oba źródła sortujemy od najtańszych, ale ich tanie
ogony są rozłączne: wakacje.pl agreguje kilkunastu organizatorów i schodzi do ~1750 zł/os,
r.pl (sam Rainbow) zaczyna od ~2680 zł/os. Zmierzone pokrycie dla `turcja-wrzesien`:

| `--limit` | dopasowań `auto` | `ambiguous` |
|---|---|---|
| 40 | 0 | 7 |
| 80 | 0 | 10 |
| 120 | 2 | 12 |
| 200 | 8 | 14 |

Stąd domyślny `--limit 200`. Przy zbyt małej próbce komenda mówi wprost, że część
wspólna jest pusta, i podpowiada większy limit.
