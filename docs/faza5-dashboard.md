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

## Hierarchia wizualna

Trzy rzeczy są ważne — **cena, termin, potwierdzona ocena** — i tylko one sięgają
po górę skali typograficznej. Reszta (lokalizacja, wyżywienie, biuro, lotnisko,
werdykt AI) jest kontekstem: mniejszy stopień, `--text-2`/`--text-3`.

- **Skala typograficzna** jest jawna i skokowa (`--fs-2xs` … `--fs-3xl`, krok
  ~1.2). Nic nie jest „domyślnym rozmiarem" — każdy element ma przypisany
  stopień, więc karta czyta się w ustalonej kolejności.
- **Przestrzeń** robi robotę separacji zamiast ramek i linii: skala `--sp-*`,
  oddech między sekcjami, karty bez cienia w motywie ciemnym.
- **Kolor niesie znaczenie, nie dekorację.** Cztery akcenty, każdy z jednym
  zastosowaniem: `--accent` = interakcja (linki, przyciski, seria wykresu),
  `--good` = spadek ceny, `--bad` = wzrost ceny i poważne zastrzeżenia,
  `--warn` = niepewna ocena / drobne uwagi. Chipy, karty i tabele są neutralne.
- Motyw jasny i ciemny przez `prefers-color-scheme`, kolory wyłącznie jako
  zmienne CSS w `:root`. Kontrast każdej pary tekst/tło ≥ 4.5:1 (WCAG AA)
  w obu motywach, na wszystkich trzech powierzchniach.
- **Mobile-first**: reguły bazowe to układ jednokolumnowy, `@media (min-width:
  46rem)` dokłada szynę ceny po prawej. Nic nie wychodzi poza ekran — szerokie
  tabele i siatka kalendarza przewijają się we własnym kontenerze
  (`overflow-x: auto`), a nie stroną.

## Ocena z wiarygodnością

Najważniejszy komponent aplikacji. Hotel ma dziś do trzech ocen — wakacje.pl
(`offer.rating`), Google i HolidayCheck (`hotel_external_rating`) — wszystkie
w skali 0–10, więc dają się porównywać wprost.

```
  8.4/10   3 053 opinie · HolidayCheck
  ▮▮▮▮▮▮▮▮ wysoka wiarygodność
  [wakacje.pl 8.5 · 42] [Google 8.6 · 1425] [HolidayCheck 8.4 · 3053]
```

Zasady:

1. **Ocena wiodąca to źródło z największą liczbą opinii** — świadomie NIE
   liczymy średniej. Średnia z „10.0 z 1 opinii" i „6.9 z 1000" ukrywa dokładnie
   tę różnicę, którą ten komponent ma pokazywać.
2. **Liczba opinii stoi obok oceny**, nigdy osobno.
3. **Wiarygodność jest zakodowana wizualnie**, w czterech stopniach liczonych
   z liczby opinii źródła wiodącego: ≥300 `high`, ≥60 `medium`, ≥10 `low`,
   poniżej `thin`. Pasek rośnie logarytmicznie (1 opinia ≈ 10%, 1000 = 100%),
   a sama liczba **cichnie**: przy `thin` jest mniejsza, w kolorze
   drugoplanowym, podkreślona kropkowaną linią i opisana „tylko 1 opinia"
   w kolorze ostrzegawczym. Dzięki temu 10.0 z jednej opinii wygląda słabiej
   niż 8.6 z 1425 — na pierwszy rzut oka, bez czytania liczb.
4. **Zgodność źródeł**: każde źródło ma swoją pastylkę z oceną i licznikiem,
   a gdy rozstęp przekroczy 1.5 pkt, pod spodem pojawia się „Źródła się
   rozjeżdżają: 8.4 (Google) vs 6.5 (HolidayCheck)".
5. **Tylko `status='ok'` jest oceną.** `ambiguous`, `no_match`, `no_rating`
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
  — karty pogrupowane po kraju. Karta: nazwa + gwiazdki, lokalizacja, **termin**,
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
- `/kalendarz` — siatka data wylotu × liczba nocy, cieplejsze tło = taniej,
  **minimum obwiedzione** (liczone w zł/os/noc — jedynej wielkości porównywalnej
  między kolumnami o różnej długości pobytu). Link w nawigacji pojawia się
  tylko wtedy, gdy `price_calendar` ma dane; sama strona odpowiada zawsze.

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

- **Self-contained**: CSS w `<style>`, wykresy jako inline SVG, zero CDN-ów,
  fontów z sieci i obrazków. Jedyne adresy `https://` to linki wychodzące
  (wakacje.pl, Google Maps, HolidayCheck) — treść, nie zależność.
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
f-stringami — bez frameworku szablonów i bez CDN, więc działa też offline.
Jedyny JavaScript w projekcie żyje w eksporcie statycznym, gdzie nie ma
serwera, który mógłby obsłużyć filtr.

Każde żądanie otwiera osobne połączenie SQLite w trybie `mode=ro` (URI), z
kilkoma próbami ponowienia przy `database is locked` — baza bywa w tym samym
czasie zapisywana przez monitor/wyszukiwanie uruchamiane równolegle.
