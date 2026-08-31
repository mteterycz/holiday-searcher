# Faza 5 — dashboard webowy i eksport statyczny

Lokalny podgląd bazy `data/offers.db` w przeglądarce. Tylko odczyt — dashboard
niczego do bazy nie zapisuje, więc może działać równolegle z monitorem (faza 2)
i z wyszukiwaniem uruchomionym w tle.

## Uruchomienie

```bash
PYTHONPATH=src python3 -m holiday_searcher.cli web                 # http://127.0.0.1:8787
PYTHONPATH=src python3 -m holiday_searcher.cli web --port 9000 --open
PYTHONPATH=src python3 -m holiday_searcher.cli export              # -> dist/
PYTHONPATH=src python3 -m holiday_searcher.cli export --out /tmp/snapshot
```

`--open` otwiera dashboard w domyślnej przeglądarce (`webbrowser.open`). Serwer
działa w bieżącym procesie (foreground) — `Ctrl+C` kończy go czysto.

## Układ modułów

`web/views.py` urósł do ponad tysiąca linii (CSS, SQL, komponenty i strony
w jednym pliku), więc został rozbity:

| moduł | odpowiedzialność |
| --- | --- |
| `web/styles.py` | tokeny CSS (`--fs-*`, `--sp-*`, `--r-*`, kolory), arkusz, szkielet strony |
| `web/urls.py` | adresy w dwóch trybach: serwerowym (`/offers?sort=…`) i statycznym (`offers.html`) |
| `web/data.py` | zapytania SQL, oceny, werdykty, kalendarz, weryfikacja ceny |
| `web/components.py` | formatowanie, wykresy SVG, komponent oceny, karta oferty |
| `web/pages.py` | strony `/`, `/offers`, `/hotels`, `/drops`, `/kalendarz`, `/offer/<key>` |
| `web/static_export.py` | `hs export` |
| `web/server.py` | `ThreadingHTTPServer`, routing, połączenie read-only |
| `web/views.py` | fasada zgodnościowa (re-eksport publicznych nazw) |

Każda strona dostaje `pages.Ctx` — niesie budowniczego adresów, flagę „czy jest
kalendarz", stopkę i ewentualny skrypt. Ten sam kod renderujący obsługuje więc
serwer i eksport; różni je wyłącznie obiekt `Urls`.

## Szata: mapa nawigacyjna / almanach portowy

Dashboard służy do **nawigowania** po ofertach i podejmowania decyzji, a temat to
Morze Śródziemne — stąd język morskich map i almanachów żeglarskich: papier mapy
zamiast bieli, atramentowy granat zamiast czerni i jeden sygnałowy pomarańcz
używany jak oznakowanie boi: rzadko i zawsze w tym samym znaczeniu.

### Paleta

| token | jasny | ciemny | rola |
| --- | --- | --- | --- |
| `--paper` | `#EEF1F0` | `#0A1620` | tło strony |
| `--surface` | `#FBFCFC` | `#11212C` | karty |
| `--surface-2` | `#E4E9E8` | `#182C39` | zagłębienia, chipy |
| `--ink` | `#10222E` | `#E4EDF1` | tekst główny |
| `--ink-2` | `#43596A` | `#A7BECC` | tekst drugoplanowy |
| `--ink-3` | `#596B79` | `#8098A8` | etykiety |
| `--line` / `--line-2` | `#D3DBDA` / `#BCC8C7` | `#243B4A` / `#33505F` | linie |
| `--control-line` | `#728A88` | `#5A7A8A` | obrys pól formularza |
| `--accent` | `#12506E` | `#5AA9CB` | **interakcja** |
| `--signal` | `#C4551F` | `#E0824C` | **sygnał treściowy** |
| `--good` / `--bad` / `--warn` | `#1E6E4B` / `#A62F2A` / `#8A5A0B` | `#4FC08D` / `#F0908A` / `#E0AE5A` | semantyka |

Dwie role koloru i **nie wolno ich mylić**:

- **`--accent` = wszystko klikalne** — linki, przyciski, aktywny filtr, seria
  wykresu, obszar pod krzywą, mapa cieplna kalendarza.
- **`--signal` = wyróżnienie TREŚCIOWE, najwyżej jedno na kartę.** Dziś dokładnie
  trzy miejsca w całej aplikacji: znacznik „najtaniej w kraju" na karcie i kropka
  przy tym samym wierszu w trybie tabeli, minimum w kalendarzu cen, oraz kafel
  „najtaniej w historii" na stronie oferty. Nic więcej. Test
  `test_signal_marks_cheapest_offer_once_per_card` pilnuje limitu.

Reszta interfejsu jest neutralna: chipy, karty i tabele nie mają koloru.

Dwa odstępstwa od wartości wyjściowych, obie wymuszone przez kontrast:

1. `--ink-3` w motywie jasnym to `#596B79`, nie `#6A7F90` — pierwotny odcień
   dawał 3.39:1 na `--surface-2`, czyli poniżej AA dla tekstu. Zachowany odcień
   i nasycenie, obniżona wyłącznie jasność do minimum domykającego 4.50:1.
2. `--signal` **nie jest używany jako tekst na papierze** (dałby 3.67:1), tylko
   jako wypełnienie z bielą (`--on-signal`, 4.50:1) albo jako element
   nietekstowy — obrys minimum w kalendarzu (3.67:1 przy progu 3:1).

Osobny `--control-line` istnieje, bo `--line-2` daje 1.4:1, a WCAG 1.4.11 wymaga
3:1 dla obrysu pola formularza, który jest jedynym nośnikiem jego granicy.
Pogrubianie wszystkich ramek kart do 3:1 zabiłoby lekkość papieru mapy.

### Typografia

Trzy kroje, trzy zadania — z **Google Fonts** (jedyny wyjątek od zasady „zero
zasobów zewnętrznych", patrz niżej):

| token | krój | zastosowanie | fallback |
| --- | --- | --- | --- |
| `--font-display` | Newsreader 500/600 | nagłówki, nazwy hoteli | `Georgia, "Times New Roman", serif` |
| `--font-ui` | Public Sans 400/500/600 | tekst, etykiety, przyciski, chipy | `system-ui, -apple-system, "Segoe UI", sans-serif` |
| `--font-mono` | IBM Plex Mono 400/600 | **wszystkie liczby** | `ui-monospace, "SF Mono", Menlo, monospace` |

Mono z `font-variant-numeric: tabular-nums` obejmuje ceny, oceny, liczby opinii,
daty, osie wykresów i komórki kalendarza — kolumny liczb układają się w pion bez
tabeli. Kroje display dostają `text-wrap: balance` i ujemny tracking na dużych
stopniach.

Skala jest jawna i skokowa (`--fs-2xs` 11px … `--fs-4xl` 40px, krok ~1.2). Nic
nie jest „domyślnym rozmiarem". Kafle statystyk i szyna ceny są wymierzone **pod
mono**: „3 559 zł" w IBM Plex Mono jest o jakieś 40% szersze niż w kroju
proporcjonalnym, więc tor siatki i stopień pisma rosną razem z ekranem, zamiast
łamać kwotę na dwie linie.

### Hierarchia — co widać z odległości metra

1. **CENA za osobę** — największa liczba na karcie (`--fs-3xl`, mono, tabular).
2. **TERMIN** (`19.09–26.09` + liczba nocy) — mono, tuż pod nazwą hotelu.
3. **OCENA z wiarygodnością** — komponent opisany niżej.

Reszta (wyżywienie, biuro, lotnisko, region) to kontekst: mniejszy stopień,
`--ink-2`/`--ink-3`, bez kolorów.

### Motyw, ruch, druk

- Pełna paleta żyje w bazowym `:root`. `@media (prefers-color-scheme: dark)` jest
  opakowane w `:root:not([data-theme="light"])`, żeby jawny wybór jasnego
  wygrywał z preferencją systemu; osobny blok `:root[data-theme="dark"]` obsługuje
  jawny wybór ciemnego. **Żaden kolor nie ma jedynej definicji wewnątrz media
  query ani `[data-theme]`** — pilnuje tego `ThemeTokensTestCase`, bo token
  zdefiniowany tylko w bloku ciemnym po prostu nie istnieje w motywie jasnym.
  `body` ma jawne tło z `var(--paper)`.
- `prefers-reduced-motion: reduce` wyłącza wszystkie przejścia.
- **Arkusz do druku** (`@media print`): czarny tekst na bieli, zero cieni,
  ukryte filtry i nawigacja, linki rozwinięte do pełnego adresu
  (`a[href^="http"]::after`), karty i wiersze bez łamania między stronami.
  `break-inside: avoid` celowo NIE obejmuje sekcji kraju — czterdziestu kart nie
  da się zmieścić na stronie, a próba kosztowałaby pustą stronę pierwszą.

### Mobile-first

Reguły bazowe to układ jednokolumnowy; `@media (min-width: 46rem)` dokłada szynę
ceny po prawej. **Nic nie wychodzi poza ekran** — zweryfikowane w headless Chrome
przy 360 px i 320 px, dla wszystkich stron, także z zablokowanym Google Fonts
(fallbacki mają inne metryki, więc to osobny przypadek).

Przyklejone paski to podatek płacony na każdym ekranie przewijania, więc rzędy,
które by się zawinęły w trzy linie, przewijają się w poziomie we własnym
kontenerze (nawigacja, przyciski sortowania, pola filtrów). Dzięki temu nagłówek
wraz z paskiem filtrów zajmuje ~38% okna telefonu zamiast 63%. Szerokie tabele
i siatka kalendarza mają własny `overflow-x: auto` — przewija się tabela, nie
strona.

## Zasoby zewnętrzne: dokładnie dwa hosty

Dashboard był wcześniej w 100% self-contained. Nowa szata sięga po trzy kroje
i to **jedyny** dopuszczony wyjątek:

- `fonts.googleapis.com` — arkusz `<link rel="stylesheet">`,
- `fonts.gstatic.com` — pliki krojów.

Wszystko inne jest błędem: CSS zostaje w `<style>`, JS w `<script>` bez `src`,
wykresy to inline SVG, obrazków nie ma. Pilnują tego
`test_external_assets_limited_to_google_fonts` (eksport) oraz
`test_pages_are_self_contained_and_themed` (serwer): parsują `href`/`src`
każdego elementu ładującego zasób i każdy `url(...)` w CSS, i przepuszczają
wyłącznie te dwa hosty.

Fallbacki są pełnoprawne, nie awaryjne — strona otwarta z dysku bez internetu ma
wyglądać poprawnie, a nie „prawie". Brak Google Fonts zmienia krój, nie układ:
weryfikacja przy 360 px i 320 px przechodzi tak samo z zablokowanymi hostami
(`--host-resolver-rules`), bo szerokości są policzone z zapasem na inne metryki
fallbacku.

## Ocena z wiarygodnością

Najważniejszy komponent aplikacji. Hotel ma dziś do trzech ocen — wakacje.pl
(`offer.rating`), Google i HolidayCheck (`hotel_external_rating`) — wszystkie
w skali 0–10, więc dają się porównywać wprost.

```text
  8.4/10   3 053 opinie · HolidayCheck
           ▪▪▪▪▪  wysoka wiarygodność
  [wakacje.pl 8.5 · 42] [Google 8.6 · 1425] [HolidayCheck 8.4 · 3053]
```

Zasady:

1. **Ocena wiodąca to źródło z największą liczbą opinii** — świadomie NIE
   liczymy średniej. Średnia z „10.0 z 1 opinii" i „6.9 z 1000" ukrywa dokładnie
   tę różnicę, którą ten komponent ma pokazywać.
2. **Liczba opinii stoi obok oceny**, nigdy osobno.
3. **Wielkość próby pokazuje SEGMENTOWANY MIERNIK**, nie ciągły pasek: pięć pól
   wypełnianych logarytmicznie (`data.confidence_segments`) — 0 opinii = 0 pól,
   1 = 1, 10 = 2, 60 = 3, 300 = 4, 1000+ = 5. Pola są policzalne jednym rzutem
   oka i porównywalne między kartami, czego ciągły pasek nie umie; podziałka
   przyrządu pasuje też do estetyki almanachu. Zero pól znaczy dokładnie „brak
   danych" i nic innego — każda oferta z choćby jedną opinią dostaje jedno pole.
4. **Wiarygodność jest zakodowana wizualnie**, w czterech stopniach liczonych
   z liczby opinii źródła wiodącego: ≥300 `high`, ≥60 `medium`, ≥10 `low`,
   poniżej `thin`. Przy `thin` sama liczba **cichnie** — mniejszy stopień
   i `--ink-3` — a obok staje etykieta „słabe dowody" w `--warn`, żeby powód
   ściszenia był nazwany, nie tylko pokazany. Dzięki temu 10.0 z jednej opinii
   wygląda słabiej niż 8.6 z 1425 na pierwszy rzut oka, bez czytania liczb.
5. **Zgodność źródeł**: każde źródło ma swoją pastylkę z oceną i licznikiem,
   a gdy rozstęp przekroczy 1.5 pkt, pod cienką kreską pojawia się „Źródła się
   rozjeżdżają: 8.4 (Google) vs 6.5 (HolidayCheck)" — wyraźnie, ale bez krzyku.
6. **Tylko `status='ok'` jest oceną.** `ambiguous`, `no_match`, `no_rating`
   i `error` znaczą „nie wiemy" i nie mogą trafić na ekran jako liczba.

Ta sama zasada rządzi resztą UI: filtr `min_rating` i sortowanie po ocenie
działają na ocenie **wiodącej**, a „TOP 5 najlepiej ocenianych" na stronie
głównej wpuszcza wyłącznie oceny `high`/`medium` — inaczej ranking wygrywałby
hotel z jedną entuzjastyczną opinią.

## Czerwone flagi AI

`red_flags` z `hotel_ai_verdict` są dzielone na **poważne** (zdrowie i
bezpieczeństwo: zatrucia, robactwo, pleśń/grzyb, kradzieże, szczury, prąd —
lista słów kluczowych w `data.SEVERE_FLAG_KEYWORDS`) i **drobne**. Poważne
dostają na karcie znacznik w kolorze `--bad` z liczbą zastrzeżeń, a na stronie
oferty baner z listą. Drobne są cichym, szarym znacznikiem i osobną listą pod
banerem — widoczne, ale nie krzyczą.

## Strony

- `/` — przegląd: najtańsza cena, liczba ofert, **liczba ocen potwierdzonych**,
  zmiany cen z 48h, snapshoty, przebiegi; rozbicie po krajach (klikalne),
  TOP 5 najtańszych, **TOP 5 najlepiej ocenianych** (tylko oceny z pokryciem),
  TOP 5 największych spadków, ostatnie przebiegi, profile.
- `/offers?sort=price|ppn|rating|drop|date&country=…&max_price=…&min_rating=…`
  — oferty pogrupowane po kraju, w **dwóch widokach** (niżej). Karta: opcjonalny
  znacznik „najtaniej w kraju", nazwa + gwiazdki, **termin**, lokalizacja,
  **blok oceny z wiarygodnością**, chipy kontekstowe, skrót werdyktu AI,
  a w prawej szynie **cena za osobę jako największa liczba**, cena za dobę,
  zmiana względem poprzedniego snapshotu i sparkline (od 3 snapshotów).
  Filtry to zwykły `<form method="get">` — linkowalne, działają bez JS.
- `/hotels?sort=price|rating|variants` — każdy hotel raz, w najtańszym wariancie,
  z liczbą wariantów i zakresem cen.
- `/offer/<key>` — kafle (cena, termin, długość, min/max, liczba snapshotów),
  duży blok oceny ze wszystkimi źródłami, historia ceny jako inline SVG +
  tabela snapshotów, **weryfikacja ceny** z `price_verification` (werdykt,
  cena z listingu vs po wejściu w ofertę, różnica %, notatka i tabela wariantów
  pokoi z `details_json`), pełny werdykt AI, tabela szczegółów.
- `/drops` — tabela obniżek (ostatnia cena vs maksimum z historii) ze sparkline.
- `/kalendarz` — siatka data wylotu × liczba nocy jako mapa cieplna zbudowana
  z **jednego odcienia** (`--accent` domieszany do tła, nie tęcza — tęcza każe
  czytać legendę zamiast siatki), a **minimum obwiedzione w `--signal`**
  i podpisane znacznikiem „min". Minimum liczone w zł/os/noc — jedynej
  wielkości porównywalnej między kolumnami o różnej długości pobytu. Link
  w nawigacji pojawia się tylko wtedy, gdy `price_calendar` ma dane; sama
  strona odpowiada zawsze.

## Praktyczność

Rzeczy, które są tu dlatego, że przy dwustu ofertach zaczynają być potrzebne —
nie dlatego, że ładnie wyglądają.

### Tryb kart i tryb tabeli

Karta jest droga w pionie; przy stu kilkudziesięciu pozycjach chce się je
przeskanować, nie przeczytać. `/offers` renderuje więc **oba widoki naraz**:
listę kart i gęstą tabelę (hotel, termin, noce, cena, zł/noc, ocena, opinie,
wyżywienie, biuro, region — wszystko w jednej linii, liczby w mono). Przełącznik
zmienia tylko `body[data-view]`, więc przełączenie jest natychmiastowe i działa
także w eksporcie statycznym.

Wybór **zapamiętuje `localStorage`** (klucz `hs.offers.view`), bo to preferencja
czytającego, a nie stan adresu. Cały dostęp do storage jest w `try/catch`:
w trybie prywatnym i przy `file://` potrafi rzucić, a strona ma wtedy po prostu
pokazać widok domyślny (karty).

Karta i wiersz tej samej oferty niosą **identyczny komplet atrybutów `data-*`**
(`offer_dataset` w `components.py`), więc jeden przebieg filtra obsługuje oba
i nie da się doprowadzić do stanu, w którym tabela pokazuje co innego niż karty.

### Pasek filtrów i licznik wyników

Pasek jest przyklejony pod nagłówkiem (`position: sticky; top: var(--stick-top)`,
gdzie `--stick-top` ustawia niewielki skrypt bazowy po zmierzeniu nagłówka — jego
wysokość zależy od kroju i szerokości, więc nie da się jej wpisać na sztywno).

Licznik pokazuje **zawężenie, nie sam wynik**: `196 → 24 oferty`. Sama liczba
24 nie mówi, czy filtr coś odciął. Serwer renderuje stan wyjściowy, a w eksporcie
JS odświeża go przy każdej zmianie filtra (`aria-live="polite"`). Gdy nic nie jest
odfiltrowane, licznik mówi po prostu `196 ofert` — strzałka pojawia się tylko
wtedy, gdy niesie informację.

Aktywne pole filtra ma obrys i etykietę w `--accent`, więc widać je bez czytania
wartości. Pusty wynik dostaje komunikat, który mówi co zrobić, a nie tylko że
nic nie ma.

### Wykresy

Duży wykres na stronie oferty: siatka, podpisane osie (`cena /os`, `data
pomiaru`), obszar pod krzywą w `--accent` z niską alfą i **wyraźnie wyróżniony
ostatni punkt** — halo, pełne kółko, kreska pionowa do osi i podpis z aktualną
ceną, bo to on odpowiada na pytanie „ile jest teraz". Gdy cała historia mieści
się w jednej dobie, oś X przełącza się z dat na godziny — trzykrotnie powtórzone
`2026-08-31` nic nie mówi.

Sparkline'y w listach mówią **tym samym językiem**: obszar pod krzywą i wyróżniony
ostatni punkt. Różni je kolor, bo kierunek zmiany ceny to osobna, ustalona rola
(`--good` spadek, `--bad` wzrost, `--accent` bez zmian).

Dla każdej komórki kalendarza bierzemy jej **najświeższe** sprawdzenie, a nie
„ostatni przebieg" jako całość: `hs kalendarz` bywa dosprawdzany dla pojedynczej
daty, więc filtr po jednym `checked_at` potrafiłby zredukować pełną siatkę do
jednej komórki. Ceną jest siatka o mieszanym wieku — dlatego nagłówek pokazuje
zakres dat sprawdzenia, gdy pomiary pochodzą z różnych dni.

## Tabele opcjonalne

`hotel_ai_verdict`, `hotel_external_rating`, `price_verification`,
`price_calendar`, `watchlist`, `notification_log` są dokładane przez inne fazy
i **każda z nich może nie istnieć**. Każdy loader w `web/data.py` sprawdza
`sqlite_master`, łapie `sqlite3.OperationalError` i zwraca pustą strukturę —
strona ma się wyrenderować zawsze. Pusta baza daje czytelny komunikat, nie
wyjątek (testy: `WebDashboardEmptyDbTestCase`,
`WebDashboardNoOptionalTablesTestCase`).

## Eksport statyczny (`hs export`)

```bash
hs export                      # -> dist/
hs export --out /tmp/snapshot  # dowolny katalog
```

Generuje `index.html`, `offers.html`, `hotels.html`, `drops.html`,
`kalendarz.html` i `offer/<key>.html` dla każdej oferty. Katalog nadaje się do
wrzucenia na GitHub Pages albo dowolny hosting statyczny — i do otwarcia wprost
z dysku.

- **Self-contained poza krojami**: CSS w `<style>`, JS w `<script>` bez `src`,
  wykresy jako inline SVG, zero CDN-ów i obrazków. Jedyne ładowane zasoby
  zewnętrzne to `fonts.googleapis.com` i `fonts.gstatic.com` (patrz „Zasoby
  zewnętrzne"); bez sieci strona nadal wygląda poprawnie, tylko innym krojem.
  Pozostałe adresy `https://` to linki wychodzące (wakacje.pl, Google Maps,
  HolidayCheck) — treść, nie zależność.
- **Adresy relatywne z rozszerzeniem** (`offers.html`, `../offers.html` ze stron
  ofert), więc działa jednakowo z `file://` i z serwera.
- **Filtry i sortowanie po stronie klienta**: parametry URL statycznie nie mają
  sensu, więc `offers.html` zawiera pełną listę i przestawia ją wbudowanym,
  bezzależnościowym JS-em (sortowanie w obrębie sekcji krajów — tak samo jak
  robi to serwer — plus filtry kraj/cena/ocena i przeliczane liczniki).
  Bez JS widać pełną listę; informuje o tym `<noscript>`.
- W stopce każdej strony jest **data wygenerowania i liczba ofert** w migawce.

## Decyzja: stdlib zamiast FastAPI

Środowisko ma tylko `httpx`, `pyyaml`, `rich` i bibliotekę standardową — bez
pip. To jednoosobowa, lokalna aplikacja odpalana ręcznie z terminala, więc
`http.server.ThreadingHTTPServer` z prostym routingiem po `self.path` w pełni
wystarcza: nie ma potrzeby ORM-a, walidacji requestów, OpenAPI ani
middleware'u, które FastAPI by tu wniósł. Strony to zwykłe HTML-e renderowane
f-stringami — bez frameworku szablonów i bez CDN, więc działa też offline
(kroje z Google Fonts degradują się do systemowych, patrz „Zasoby zewnętrzne").
JavaScriptu jest tyle, ile trzeba i ani linijki więcej: skrypt bazowy mierzy
nagłówek dla przyklejonego paska, przełącznik widoku pamięta wybór
w `localStorage`, a filtrowanie i sortowanie po stronie klienta żyje wyłącznie
w eksporcie statycznym, gdzie nie ma serwera, który mógłby obsłużyć query
string.

Każde żądanie otwiera osobne połączenie SQLite w trybie `mode=ro` (URI), z
kilkoma próbami ponowienia przy `database is locked` — baza bywa w tym samym
czasie zapisywana przez monitor/wyszukiwanie uruchamiane równolegle.
