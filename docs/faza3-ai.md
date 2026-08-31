# Faza 3 — opinie z wakacje.pl + ocena AI hoteli (Gemini)

## Co robi ta faza

Scoring z fazy 1 odpowiada na pytanie **„czy to jest tanio"**. Faza 3 dokłada
drugie pytanie: **„czy tam jest dobrze"** — na podstawie opinii podróżnych,
a nie wiedzy modelu o świecie.

```
oferty (faza 1) ──► scoring ──► top N HOTELI
                                    │
                       opinie z wakacje.pl (SSR HTML)
                                    │
                      Gemini (structured output, grounding)
                                    │
                    werdykt hotelu ──► cache permanentny
                                    │
                       vibe match (jedno wywołanie na shortlistę)
```

## Trzy decyzje, które trzymają tę fazę w ryzach

### 1. AI ocenia HOTELE, nie oferty

Oferta zmienia się co dzień (cena, termin, biuro, typ pokoju); hotel nie.
Ocenianie ofert znaczyłoby płacenie za ten sam werdykt kilkanaście razy w tygodniu.
Klucz cache'u to `(hotel_id, prompt_version, model)`, a cache jest **permanentny** —
nie ma TTL-a, bo hotel nie psuje się w tydzień.

`input_hash` (skrót materiału opinii) jest **zapisywany, ale nie jest częścią klucza**.
Gdyby był, każda nowa opinia generowałaby nowy request — a jedna opinia więcej
nie zmienia obrazu hotelu. Odświeżenie jest jawne: `get_or_create(..., refresh=True)`.

### 2. Grounding: model nie wolno mu wiedzieć nic sam z siebie

Gemini zna te hotele z internetu i bardzo chce się tą wiedzą podzielić. Wtedy
ocena przestaje mieć cokolwiek wspólnego z opiniami, na których miała stać.
Dlatego prompt zabrania tego wprost, a **schema odpowiedzi dopuszcza `null`
w każdym polu oceny** — brak informacji jest poprawną odpowiedzią, zgadywanie nie.
`generationConfig.responseSchema` + `responseMimeType: "application/json"` sprawiają,
że nie parsujemy prozy ani nie wycinamy ```` ```json ```` z odpowiedzi.

Skala `noise` jest odwrócona intuicyjnie: **5 = cicho**, 1 = hałaśliwie. Dzięki temu
wszystkie pięć ocen ma ten sam kierunek („więcej = lepiej") i tabela się nie myli.

### 3. Werdykty z różnych modeli nie są porównywalne

Model jest częścią klucza cache'u i jest zapisywany przy każdym werdykcie.
Jeden przebieg rankingowy = jeden model: `VerdictService.get_or_create` domyślnie
woła `pool.acquire(role, strict=True)`, czyli **bez failoveru**. Wyczerpany limit
oznacza brak werdyktu, a nie po cichu podmieniony model.

## Pula modeli i limity

Każdy model ma **osobny** limit — to nie jest wspólna pula konta.

| Model | RPM | RPD | TPM | Rola | Do czego |
|---|---:|---:|---:|---|---|
| `gemini-3.5-flash-lite` | 15 | 500 | 250K | `bulk-misc` | normalizacja, dedup |
| `gemini-3.1-flash-lite` | 15 | 500 | 250K | `bulk-verdict` | **werdykty hoteli** |
| `gemini-3.5-flash` | 5 | 20 | 250K | `deep` | **vibe match** |
| `gemini-3.6-flash` | 5 | 20 | 250K | `deep` (failover) | zapas dla vibe |
| `gemini-3.7-flash` | 5 | 20 | 250K | `experimental` | nieużywany w produkcji |

Nazwy modeli są stałymi w `ai/pool.py` (`MODELS`, `ROLE_CHAINS`) — mogą wymagać
korekty do realnych identyfikatorów API bez ruszania reszty kodu.

- **RPD** liczony w SQLite (`ai_usage(model, day, requests)`), więc przeżywa restart.
  `acquire()` liczy request w momencie rezerwacji — wołaj bezpośrednio przed wysłaniem.
- **RPM** trzymany w pamięci (okno 60 s) i egzekwowany sleepem. Okno minutowe wygasa
  szybciej niż typowa przerwa między uruchomieniami, więc nie ma sensu go utrwalać.
- `hs ai-usage` pokazuje zużycie per model per dzień.

### Budżet requestów

| Operacja | Wywołań | Model | Uwagi |
|---|---:|---|---|
| `hs enrich --top 10`, pierwszy raz | ≤10 | bulk-verdict | 1 na hotel; ~12 tys. znaków promptu |
| `hs enrich --top 10`, kolejny raz | 0 | — | wszystko z cache'u |
| `hs vibe` | 1 | deep | **jedno** wywołanie na całą shortlistę |
| Pobranie opinii | 1 GET/hotel | — | wakacje.pl, delay ≥1.5 s |

500 werdyktów dziennie starcza na ~50 przebiegów `enrich --top 10` z samych nowych
hoteli; realnie po tygodniu prawie wszystko leci z cache'u. Model `deep` ma RPD=20,
dlatego vibe **musi** być jednym wywołaniem — pętla po hotelach spaliłaby limit
w jednym przebiegu.

## Rekonesans: skąd brać opinie

Endpointy z fazy 0 okazały się tylko częściowo prawdziwe. Wszystko poniżej
sprawdzone empirycznie na hotelach z `data/offers.db`
(`hotelId` 35267 „Arsi Paradise Beach", 35291 „Mysea Alara"), delay 1.6 s.

Baza: `POST https://www.wakacje.pl/v2/api/<method>`, nagłówki jak w
`providers/wakacje.py` (UA przeglądarki, `Referer`, `Origin`). Auth: brak.

| Endpoint | Wynik |
|---|---|
| `/v2/api/getOpinionsBox` | **działa**, ale tylko agregaty |
| `/v2/api/getOpinions` | **działa**, treści opinii (nie było go w fazie 0) |
| `/v2/api/newOpinions/` | **nie istnieje** — proxy 404 |
| `/v2/api/getPlusesMinuses/` | **nie istnieje** — proxy 404 |
| `/v2/api/getHotelDescription` | istnieje, payload nieodgadnięty (upstream 400) |

### Payload: komplet trzech pól, nie sam hotelId

Proxy `/v2/api` przepuszcza żądanie do `http://api.wakacje.dc-2.lb.dcwp.pl/v2/<grupa>/<method>`
i **zwraca jego kod błędu w treści** — to on zdradził, czego brakuje:

```jsonc
// {"hotelId": 35267}                     -> upstream 404 (pole rozpoznane, zasób nie)
// {"objectId": 35267}                    -> upstream 400 (pole nierozpoznane)
// [{"method": "...", "params": {...}}]   -> upstream 400 (to NIE jest RPC jak /offers)
// {"hotelId": 35267, "objType": "H", "brand": "WAK"}   -> success ✅
```

Czyli: `hotelId` jest poprawną nazwą pola, ale **wymagany jest komplet**
`hotelId` + `objType: "H"` + `brand: "WAK"`. W przeciwieństwie do `/v2/api/offers`
te metody biorą **płaski obiekt**, a nie tablicę `[{method, params}]`.

### `getOpinionsBox` — same agregaty

```jsonc
// POST /v2/api/getOpinionsBox
// {"hotelId": 35267, "objType": "H", "brand": "WAK", "limit": 5}
{"success": true, "type": "info", "msg": "getOpinionsBox", "data": {
  "opinionsCount": 1, "ourClientsOpinionsCount": 0, "ratingRecommends": 100,
  "holidayCheckRecommendation": null, "holidayCheckRate": null,
  "opinionType": "ten hotel", "ratingValue": 5.3, "isAfter2013": true,
  "reservationCount": 137}}
```

Zero treści opinii — do werdyktu bezużyteczny. Zostawiony jako `fetch_box()`
do sanity-checku, czy w ogóle jest co pobierać.

### `getOpinions` — treści, ale tylko ze starego systemu

```jsonc
// POST /v2/api/getOpinions  (ten sam payload)
{"success": true, "data": {"opinions": [{
  "id": 314755, "type": "hotele", "name": "Janusz", "tripDate": "2021-11-01",
  "willRecommend": true, "isClient": false, "rank": 6.5,
  "note": "Byliśmy tam dwa tygodnie…",
  "advantage": "Jest po remoncie blisko plaży…",
  "defect": "…bliskość drogi, cienkie ściany…",
  "kindOfTrip": "Para", "recommendedTo": ["Ceniących spokój"],
  "photos": [...]}], "hasOurClients": false, "opinionsCount": 0}}
```

Struktura idealna, ale zawartość nie: dla hotelu 35267 zwraca **1 opinię z 2021**,
podczas gdy serwis pokazuje **20 zweryfikowanych**. To pula sprzed obecnego systemu
opinii. Zostawione jako `fetch_api()` — droga zapasowa.

### Endpointy, których nie ma

```jsonc
// POST /v2/api/newOpinions/     -> 404 {"error":"Not found","path":"/v2/api/newOpinions/"}
// POST /v2/api/getPlusesMinuses/-> 404 {"error":"Not found","path":"/v2/api/getPlusesMinuses/"}
```

404 leci z samego proxy (inny kształt błędu niż upstreamowy), niezależnie od payloadu,
z ukośnikiem końcowym i bez. Sprawdzono też `getNewOpinions` i `getHotelPlusesMinuses` — też 404.
**Plusy i minusy nie mają osobnego endpointu** — są polami `advantage` / `defect`
przy każdej opinii. Nazwy z fazy 0 pochodziły najpewniej z poprzedniej wersji serwisu.

### `getHotelDescription` — świadomie niedokończony

Metoda istnieje (proxy przepuszcza do `/v2/hotels/getHotelDescription`), ale
**każda zgadnięta kombinacja pól kończy się upstreamowym 400**. Sprawdzone:
`{hotelId}`, `{hotelId, brand}`, `{hotelId, objType, brand}`,
`{hotelId, objType, objCode, tourOpCode}`.

`WakacjeOpinions.fetch_description()` rzuca `NotImplementedError` z odsyłaczem tutaj.
Dokończyć da się dopiero po podejrzeniu realnego requestu przeglądarki (DevTools →
Network na stronie hotelu). Nie blokuje to fazy 3: opis hotelu i tak jest w HTML-u,
a werdykt ma stać na opiniach, nie na marketingowym opisie.

### Właściwe źródło: strona opinii (SSR)

```
GET https://www.wakacje.pl/opinie/hotele/{slug}-h{hotelId}.html
```

Strona jest renderowana **po stronie serwera** i zawiera inline
`<script>var opinions = [...]</script>` z czystym JSON-em:

```jsonc
{"authorName": "Remigiusz", "rate": 7.71,
 "tripDateAt": {"date": "2026-08-01 00:00:00.000000"},
 "note": "Hotel ok, mały basen…", "advantage": "Blisko plaży",
 "defect": "Insekty - karaluchy", "kindOfTrip": "Rodzina z dziećmi",
 "isClient": true, "opinionUserCategoryId": 1}
```

Uwagi z parsowania:

- Bloków `var opinions` są **dwa**: krótki podgląd (bez `rate`) i pełny. Bierzemy ten
  z polem `rate` — stąd sortowanie po długości i sprawdzenie klucza w `parse_opinions_page`.
- Oceny cząstkowe (Ogólne wrażenia / Hotel / Położenie / Pokoje / Wyżywienie /
  Atrakcje dla dzieci / Sport i rozrywka) są w HTML-u: `item__title` + `score`.
- `slug` bierzemy z `Offer.url` (`slug_from_url`), bo z samego `hotelId` adresu nie złożysz.
  **Uwaga:** adres z fazy 1 (`/hotele/{slug}/`) zwraca 404 — poprawne formy to
  `/opinie/hotele/{slug}-h{id}.html` i `/hotele/{kraj}/{slug}-{id}.html`.
  Do parsowania opinii potrzebny jest tylko slug, więc nie ruszamy adaptera.
- Hotel bez opinii (np. 270560 „Club Bayar") zwraca 200 bez bloku danych — to normalny
  stan, nie awaria: `HotelOpinions.ok == False`, `error = "brak opinii na stronie"`.
- Strona oddaje ~20 najnowszych opinii. Do werdyktu i tak bierzemy maks. 25
  (`build_verdict_user`), więc paginacja nie jest potrzebna.

**Jeden GET zamiast trzech POST-ów, komplet danych, bez parsowania prozy.**

## Użycie

```bash
# ocena AI top 5 hoteli profilu (bez klucza: pokaże surowe opinie)
PYTHONPATH=src python3 -m holiday_searcher.cli enrich turcja-wrzesien --top 5

# dopasowanie do pola `vibe` z profilu — jedno wywołanie modelu 'deep'
PYTHONPATH=src python3 -m holiday_searcher.cli vibe turcja-wrzesien

# ile limitu zjedzone dziś (per model)
PYTHONPATH=src python3 -m holiday_searcher.cli ai-usage
```

`hs enrich` bierze oferty z `data/offers.db` (filtry profilu, cena z najnowszego
snapshotu), a gdy baza jest pusta — pobiera świeże przez `WakacjeProvider`.
`--fresh` wymusza pobranie z pominięciem bazy.

`hs vibe` wymaga pola `vibe` w profilu, np.:

```yaml
vibe: "spokojny hotel przy szerokiej piaszczystej plaży, dobre jedzenie,
       bez animacji do północy, z dala od głównej drogi"
```

## Klucz API

```bash
cp config/.env.example config/.env      # jeśli jeszcze nie ma
# i wpisz:
GEMINI_API_KEY=AIza...
```

Klucz: <https://aistudio.google.com/apikey>. Można też podać przez zmienną
środowiskową `GEMINI_API_KEY` — `config.get_secret` sprawdza najpierw środowisko,
potem `config/.env`. Plik `.env` nie jest nigdzie wysyłany.

### Zachowanie bez klucza (graceful degradation)

Nic się nie wywala. `hs enrich` pobiera opinie i pokazuje je w tabeli
(plusy/minusy prosto z wakacje.pl — dokładnie ten materiał, który dostałby model)
plus komunikat, ilu hoteli dotyczyłaby ocena. `hs vibe` mówi, ile hoteli ma już
werdykt w cache'u i czego brakuje. `hs ai-usage` działa zawsze — to tylko odczyt bazy.
To samo dotyczy wyczerpanego limitu dziennego i błędu Gemini: brak werdyktu
to brak danych, a nie awaria przebiegu.

## Tabele w bazie

```sql
CREATE TABLE ai_usage (          -- licznik dzienny, per model
    model TEXT, day TEXT, requests INTEGER, PRIMARY KEY (model, day));

CREATE TABLE hotel_ai_verdict (  -- cache werdyktów, permanentny
    hotel_id TEXT, provider TEXT, model TEXT, prompt_version INTEGER,
    input_hash TEXT, verdict_json TEXT, created_at TEXT,
    PRIMARY KEY (hotel_id, prompt_version, model));
```

Obie zakładane leniwie przez `ai/pool.py` i `ai/verdicts.py` — `storage.py` z fazy 1
pozostaje nietknięty.

## Wersjonowanie promptu

`PROMPT_VERSION` w `ai/prompts.py` jest częścią klucza cache'u. **Każda zmiana
treści promptu albo schematu musi podbić tę liczbę.** Inaczej werdykt wygenerowany
w innym reżimie zostanie podany jako świeży i nikt się nie zorientuje.
Podbicie nie kasuje starych werdyktów — po prostu przestają być używane.

## Testy

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Bez sieci: klient Gemini podmieniony (`FakeClient` / `httpx.MockTransport`),
opinie budowane w pamięci, baza w katalogu tymczasowym. Pokryte:
egzekwowanie RPD i failover wg roli, RPM przez sleep, osobne limity per model,
trafienie w cache bez drugiego wywołania, unieważnienie cache'u przez
`PROMPT_VERSION`, `null` przeżywający zapis i odczyt, retry 1× na 429,
degradacja bez klucza / bez limitu / bez opinii, parsowanie strony opinii.
