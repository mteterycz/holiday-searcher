# Kalendarz cen (`hs kalendarz`)

Lista TOP-15 odpowiada na pytanie „który hotel". Kalendarz odpowiada na inne:
**kiedy i na ile dni** lecieć, żeby zapłacić mniej za to samo. Profil ma sztywne
okno terminów, rynek nie — a różnica między sąsiednimi datami wylotu bywa
większa niż różnica między hotelami.

```bash
PYTHONPATH=src python3 -m holiday_searcher.cli kalendarz wrzesien-okazje --spread 4 --limit 60
PYTHONPATH=src python3 -m holiday_searcher.cli kalendarz wrzesien-okazje --hotel 23141
```

Wynik: siatka, w której **wiersz = data wylotu**, **kolumna = liczba nocy**,
a komórka to najtańsza oferta pasująca do reszty filtrów profilu (gwiazdki,
ocena, wyżywienie, lotnisko, sufit ceny). Pod siatką jedno zdanie
podsumowania i rozbicie „ile daje samo przesunięcie daty".

## Parametry

| flaga           | domyślnie | znaczenie                                                               |
| --------------- | --------- | ----------------------------------------------------------------------- |
| `--spread N`    | 5         | ile dni **przed i po** oknie profilu dołożyć do sprawdzenia             |
| `--limit N`     | 60        | ile ofert pobierać na jedną datę wylotu (w trybie `--hotel`: na całość) |
| `--hotel ID`    | —         | kalendarz dla jednego hotelu (`hotel_id`)                               |
| `--delay S`     | 1.5       | przerwa między zapytaniami; wartości poniżej 1.5 s są podnoszone        |
| `--max-dates N` | 24        | twardy sufit liczby sprawdzanych dat — bezpiecznik kosztu zapytań       |
| `--no-save`     | —         | nie zapisuj wyniku do `price_calendar`                                  |

## Trzy decyzje projektowe, które łatwo zepsuć

### 1. Jedno zapytanie na jedną datę wylotu

Kuszące jest odpytanie API raz, dla całego szerokiego okna. Nie działa:
wyniki są sortowane po cenie rosnąco, więc przy limicie 60 dostajemy 60
najtańszych ofert **z całego okna** — wszystkie skupione w jednym–dwóch
najtańszych terminach, a reszta siatki zostaje pusta. Dlatego dla każdej daty
`D` budujemy osobny wariant profilu:

```python
dataclasses.replace(profile, date_from=D, date_to=D + timedelta(nights_max + 1))
```

`SearchProfile` jest `frozen`, więc `replace` jest tu jedyną poprawną drogą —
i przy okazji gwarantuje, że oryginalny profil nigdy się nie zmieni.
`date_to` to granica **powrotu**, nie wylotu, stąd `+ nights_max` (i jeden dzień
luzu, bo część touroperatorów liczy dobę powrotu inaczej).

Koszt: liczba dat × liczba kierunków zapytań. Dla `wrzesien-okazje`
(5 kierunków) i `--spread 4` to ok. 100 zapytań, czyli kilka minut przy
grzecznym `--delay 1.5`. Sufit `--max-dates` przycina okno **symetrycznie
wokół środka**, żeby budżet nie rósł z rozmiarem profilu, a to, co zostanie,
było nadal wyśrodkowane na terminie użytkownika.

### 2. Minimum liczone w zł/os/noc, nie w cenie całkowitej

W komórce pokazujemy cenę całkowitą za osobę (to, co użytkownik płaci), ale
**globalne minimum wyznacza `Offer.price_ppn`**. Gdyby wyznaczała je cena
całkowita, zwycięzcą zawsze byłby najkrótszy pobyt i siatka nie niosłaby żadnej
informacji — 5 nocy jest tańsze od 7 z definicji, nie z okazji.

Stąd trzy poziomy podświetlenia:

- **zielony** — globalne minimum (w zł/os/noc);
- **jasnozielony** — komórki w granicach 5% od minimum: terminy „równie dobre",
  czyli takie, w których użytkownik ma swobodę wyboru;
- **pogrubienie** — najtańszy termin **w obrębie jednej kolumny**. To
  porównanie jabłek z jabłkami: ta sama długość pobytu, różna data. Ono właśnie
  pokazuje czysty efekt przesunięcia wylotu.

Daty spoza okna profilu są wyszarzone — użytkownik musi widzieć, które wiersze
są bonusem z `--spread`, a które faktycznie mieszczą się w jego terminie.

### 3. Punktem odniesienia jest najtańszy termin Z OKNA PROFILU

Zdanie „o 480 zł taniej niż w terminie z profilu" porównuje minimum globalne
z **najtańszą** komórką o tej samej liczbie nocy wewnątrz oryginalnego okna.
Dwa świadome zawężenia:

- **ta sama liczba nocy** — porównywanie 5 nocy z 11 nocami dałoby efektowną,
  ale fałszywą oszczędność;
- **najtańsza, nie pierwsza z brzegu** — inaczej wystarczyłoby, żeby w oknie
  profilu trafił się jeden drogi termin, i „oszczędność" byłaby zawyżona.

Jeśli minimum i tak wypada w oknie profilu, komunikat mówi to wprost i zamiast
oszczędności pokazuje rozrzut wewnątrz samego okna.

## Tryb `--hotel`

`hs kalendarz <profil> --hotel 23141` rysuje tę samą siatkę dla jednego
obiektu. Identyfikator bierze się z bazy:

```bash
sqlite3 data/offers.db "SELECT hotel_id, hotel_name FROM offer LIMIT 5"
```

API przyjmuje `params.hotelId: ["<id>"]`. Payload budujemy **we własnym
module** (`price_calendar.hotel_payload`), nie w adapterze — provider nie zna
pojęcia „jeden hotel" i dokładanie tam parametru zmieniałoby moduł, z którego
korzystają wszystkie pozostałe komendy. Mapowanie odpowiedzi reużywamy
z providera (`WakacjeProvider._map`), bo to jedyne miejsce w projekcie, które
wie, jak wygląda oferta wakacje.pl.

Dwie różnice względem trybu profilowego, obie celowe:

- `countryId`/`regionId` są **puste** — hotel jednoznacznie wskazuje kraj,
  a filtr kraju mógłby go tylko wyciąć, gdyby profil był wielokierunkowy;
- filtr `rating_min` nie jest stosowany — użytkownik sam wskazał hotel, więc
  ukrywanie jego terminów z powodu progu ocen byłoby nieuprzejme. Filtr
  lotniska zostaje (API grupuje Chopina i Modlin pod jednym id — rozróżnia je
  dopiero `departurePlaceCode` z odpowiedzi).

## Zapis do bazy

Moduł zakłada własną tabelę (idempotentnie, przez `executescript`):

```sql
CREATE TABLE IF NOT EXISTS price_calendar (
    profile        TEXT NOT NULL,
    hotel_id       TEXT NOT NULL DEFAULT '',   -- '' = cały profil
    departure_date TEXT NOT NULL,
    nights         INTEGER NOT NULL,
    price_pp       INTEGER NOT NULL,
    price_ppn      REAL NOT NULL,
    checked_at     TEXT NOT NULL,
    PRIMARY KEY (profile, hotel_id, departure_date, nights, checked_at)
);
```

`checked_at` jest **częścią klucza głównego**, więc kolejne przebiegi dokładają
nowy przebieg zamiast kasować poprzedni — tak samo jak `price_snapshot`.
Dzięki temu da się później zapytać nie tylko „kiedy jest taniej", ale też „czy
ten kalendarz się przesuwa". Odczyt: `price_calendar.load_calendar(db, profil,
hotel_id="", checked_at=None)` — bez `checked_at` zwraca najświeższy przebieg.

`hotel_id` dla całego profilu to pusty string, nie `NULL`: `NULL` w kluczu
głównym SQLite nie porównuje się tak, jak by się chciało.

Baza jest współdzielona z monitorem z launchd i dashboardem, więc moduł otwiera
**krótkie połączenia** (`_connect`, timeout 30 s) i zamyka je natychmiast po
zapisie.

## Co pokazał pierwszy przebieg na żywo

`--spread 4 --limit 60`, profil `wrzesien-okazje`, 20 dat wylotu, ok. 100
zapytań, ~21 minut:

| nocy | najtaniej         | najdrożej        | różnica          |
| ---- | ----------------- | ---------------- | ---------------- |
| 5    | 02.10 — 1 629 zł  | 22.09 — 2 591 zł | 962 zł/os (37%)  |
| 7    | 30.09 — 1 842 zł  | 18.09 — 2 829 zł | 987 zł/os (35%)  |

Wniosek: teza użytkownika („2–3 dni potrafią zbić cenę o 20–30%") jest
prawdziwa, a nawet zachowawcza — rozrzut w obrębie tej samej długości pobytu
sięgnął 35–37%. Widać przy tym dwie prawidłowości: cena rośnie ku środkowi
okna (druga połowa września jest najdroższa) i systematycznie spada pod koniec
sezonu. Minimum wypadło 30.09, czyli na samej krawędzi okna z profilu —
to argument, żeby okno w `config/profiles.yaml` przesunąć w październik,
a nie tylko przebierać w hotelach.

## Znane ograniczenie: puste komórki

Siatka pokazuje najtańszą ofertę, jaką **znaleziono w próbce**, a nie pełny
cennik. Zapytanie dla daty `D` obejmuje okno `D … D+nights_max` i jest
sortowane po cenie, więc przy `--limit 60` (12 ofert na kierunek) najtańsze
wyniki potrafią skupić się w kilku terminach, a inne komórki zostają puste
(`·`). Pusta komórka znaczy więc „nic taniego nie wypłynęło", a nie „nie ma
lotu". Lekarstwo jest jedno: większy `--limit` (kosztem czasu — to liniowo
więcej stron do pobrania).

W praktyce widać też, że rynek sam ogranicza kolumny: czartery chodzą
w cyklach 7-nocnych, więc kolumny 5 i 7 nocy są gęste, a 6 i 8–11 nocy niemal
puste. To nie jest błąd pobierania.

## Testy

```bash
PYTHONPATH=src python3 -m unittest tests.test_price_calendar -v
```

Bez sieci: provider zamockowany (`FakeProvider`), baza w `tempfile`.
Pokrycie: budowanie okna dat (w tym symetryczne przycinanie i odrzucanie
przeszłości), niemutowalność profilu przy `replace`, agregacja minimum po
(data, noce), rozróżnienie minimum w zł/os/noc od minimum ceny całkowitej,
strefa 5%, treść podsumowania, kształt payloadu dla `--hotel`, oraz
zapis/odczyt tabeli wraz z rozdzieleniem wierszy profilu i hotelu.
