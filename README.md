# holiday-searcher

Prywatna wyszukiwarka ofert wakacyjnych: pobiera oferty z biur podróży,
normalizuje je do porównywalnej postaci, ocenia i (docelowo) pilnuje obniżek.

## Uruchomienie

```bash
export PYTHONPATH=src   # albo prefiksuj każdą komendę
python3 -m holiday_searcher.cli count    turcja-wrzesien   # ile ofert pasuje
python3 -m holiday_searcher.cli search   turcja-wrzesien --limit 150 --top 15
python3 -m holiday_searcher.cli diff     turcja-wrzesien   # zmiany cen między przebiegami
python3 -m holiday_searcher.cli monitor  turcja-wrzesien --dry-run  # cykl dla launchd
python3 -m holiday_searcher.cli compare  turcja-wrzesien   # wakacje.pl vs r.pl (domyślnie --limit 200)
python3 -m holiday_searcher.cli enrich   turcja-wrzesien --top 5    # opinie + werdykty AI
python3 -m holiday_searcher.cli vibe     turcja-wrzesien   # dopasowanie do pola `vibe`
python3 -m holiday_searcher.cli ai-usage                   # zużycie limitów Gemini
python3 -m holiday_searcher.cli web --open                 # dashboard http://127.0.0.1:8787
python3 -m holiday_searcher.cli stats
```

Filtry: `config/profiles.yaml`. Baza: `data/offers.db` (SQLite).
Zależności: `httpx`, `pyyaml`, `rich` (dashboard: czysta biblioteka standardowa).
Testy: `PYTHONPATH=src python3 -m unittest discover -s tests`.

### Konfiguracja (opcjonalna — bez niej wszystko działa w trybie zdegradowanym)

`cp config/.env.example config/.env` i uzupełnij:

- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — powiadomienia (instrukcja: `docs/faza2-monitoring.md`).
  Bez nich `monitor` wypisuje powiadomienia na konsolę/do logu. Uwaga: konsolowy fallback
  też zużywa cooldown anti-spamu — ustaw token PRZED włączeniem harmonogramu.
- `GEMINI_API_KEY` — werdykty AI (`docs/faza3-ai.md`). Bez klucza `enrich` pokazuje
  surowe opinie, `vibe` odmawia z komunikatem.

Harmonogram 3×/dzień (08:00, 14:30, 20:30): `bash scripts/install-launchd.sh`
(świadomie nieaktywowany automatycznie). Logi: `data/monitor.log`.

Dokumentacja modułów: `docs/faza2-monitoring.md`, `docs/faza3-ai.md`,
`docs/faza4-drugie-zrodlo.md`, `docs/faza5-dashboard.md`.

## Ustalenia fazy 0 (rekonesans wakacje.pl)

Serwis to Next.js; oferty ładowane po stronie klienta. Nie trzeba Playwrighta.

|               |                                                                   |
| ------------- | ----------------------------------------------------------------- |
| Endpoint      | `POST https://www.wakacje.pl/v2/api/offers`                     |
| Baza URL      | składana w bundlu jako`["/v2","/api"].join("")`                |
| Payload       | tablica z jednym`{method: "search.tripsSearch", params: {...}}` |
| Odpowiedź    | `{success, data: {count, offers: [...]}}`                       |
| Auth          | brak — działa bez ciasteczek i tokenów                         |
| Cloudflare    | nie napotkano                                                     |
| Stronicowanie | `query.pageNumber`, rozmiar strony przez `params.limit`       |
| Słowniki     | `https://www.wakacje.pl/v2/_data/dictionary.js`                 |

**Cena jest za osobę** — zweryfikowane empirycznie: przy 2/3/4 dorosłych wartość
zostaje w tym samym rzędzie wielkości (dla 3 os. spada, bo trzecia osoba w pokoju
jest tańsza). Gdyby to była cena całkowita, przy 4 osobach byłaby dwukrotnie wyższa.
To fundament całej normalizacji — każda nowa integracja musi przejść ten sam test.

Kody słownikowe (`dictService`): 1=All Inclusive, 2=HB, 3=BB, 6=FB,
9=Ultra AI, 10=AI Soft, 44=AI Plus. Sortowanie: 1=najtańsze, 13=popularne, 11=ocena.
Turcja `countryId=16`; Riwiera Turecka `312009`, Wybrzeże Egejskie `312173`.

Pole `ratingValue` zwraca `0.0` dla hoteli bez opinii — to brak danych, nie ocena zero.
Adapter mapuje to na `None`.

## Jak liczona jest ocena

```
score = 0.45·cena_vs_koszyk + 0.30·jakość + 0.10·wyżywienie + 0.15·promocja
```

Koszyk porównawczy = ten sam region, ta sama kategoria gwiazdkowa, ta sama rodzina
wyżywienia. Porównanie zawsze w **zł za osobę za noc**.

Dwie decyzje, które łatwo zepsuć:

- **Mediana koszyka liczona jest z osobnej próbki referencyjnej** (`--reference`,
  sortowanie po popularności), nie z pobranych kandydatów. Kandydaci to najtańsze
  oferty, więc mediana z nich czyniłaby okazją dosłownie każdą pozycję.
- **Hotel bez opinii nie może przebić hotelu z dobrą oceną.** Brak ocen daje jakość
  ograniczoną do `UNRATED_CAP` (0.55 × gwiazdki), zamiast fallbacku do pełnych gwiazdek.

### Znane ograniczenie

Mnożnik „vs koszyk" jest poprawny jako **porządek**, ale zawyżony jako **wartość
bezwzględna**: kandydaci pochodzą z taniego ogona, a referencja z ofert popularnych
(te skłaniają się ku droższym). API nie udostępnia losowania z populacji.
Traktuj 1.6× jako „wyraźnie taniej niż typowa oferta w tym koszyku", nie jako
dokładne 60% poniżej rynku.

## Ustalenia fazy 4 (r.pl / Rainbow)

`POST https://r.pl/api/wyszukiwarka/v5.0/wyszukaj` + `POST /api/bloczki/v5.0/pobierz-bloczki`
(API rozbite: pierwsze daje id+ceny, drugie dokłada nazwy/gwiazdki/wyżywienie).
**Domyślnie cena jest ZA CAŁĄ GRUPĘ** — atrybut `Cena:["avg","*-*"]` przełącza na
cenę za osobę (flaga `CzyCenaZaOsobe` w odpowiedzi; adapter dzieli sam, gdyby wróciła
`false`). Walidacja krzyżowa: Asrin Beach ma identyczną cenę/os u obu dostawców.
r.pl nie rozróżnia UAI/AI Plus/AI Soft (wszystko → AI), ocena w skali 1–6 (adapter
przelicza ×10/6). Szczegóły i ograniczenia: `docs/faza4-drugie-zrodlo.md`.

## Stan

- [X] Faza 0 — rekonesans wakacje.pl
- [X] Faza 1 — adapter, normalizacja, SQLite, scoring, CLI
- [X] Faza 2 — detekcja obniżek, Telegram, launchd (`diff`, `monitor`)
- [X] Faza 3 — opinie + ocena AI Gemini (`enrich`, `vibe`, `ai-usage`)
- [X] Faza 4 — drugie źródło r.pl + deduplikacja hoteli (`compare`)
- [X] Faza 5 — dashboard webowy (`web`)

Do aktywacji przez użytkownika: `config/.env` (Telegram, Gemini) i `scripts/install-launchd.sh`.
