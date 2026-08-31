# Weryfikacja ceny końcowej (wakacje.pl)

Rekonesans przeprowadzony 2026-08-31. Kod: `src/holiday_searcher/verify.py`,
`src/holiday_searcher/cli_ext/verify.py`. Testy: `tests/test_verify.py`.

## Po co

Listing (`search.tripsSearch`) zwraca cenę opisaną w serwisie jako „od".
Cały ranking cenowy projektu — mnożnik „vs koszyk", detekcja obniżek,
`price_ppn` — stoi na założeniu, że ta cena jest prawdziwa. Do tej pory
nikt tego nie sprawdził. Pytanie brzmiało: czy po wejściu w ofertę
i wybraniu pokoju cena rośnie?

**Odpowiedź: nie. Cena „od" to najtańszy REALNIE rezerwowalny wariant pokoju,
co do złotówki.** Szczegóły niżej.

## Odkryte endpointy

Wszystkie pod `https://www.wakacje.pl/v2/api`, nagłówki jak w
`providers/wakacje.py` (User-Agent przeglądarkowy, `Referer`/`Origin` na
`https://www.wakacje.pl`). Brak auth, brak ciasteczek, brak Cloudflare.
Koperta odpowiedzi jak wszędzie: `{success, type, msg, datetime, data}`.

| Endpoint | Metoda | Werdykt |
| --- | --- | --- |
| `getOfferDataFromMerlin` | POST | działa, ale **bez ceny** — ślepy zaułek |
| `getInitOfferData/{offerId}/` | GET | **działa** — parametry oferty + świeża cena „od" |
| `getCalculatorOfferVariants/{offerId}` | POST | **działa — to jest źródło ceny końcowej** |
| `getOfferInfoBox` | POST | działa — te same dane co init, ale w POST |
| `getOfferAvailability/{tourOp}/{roomId}` | POST | działa — potwierdza `isPerPerson` |
| `offerConfiguratorV2/offerVariants` | POST | działa, ale operuje na innym modelu (patrz niżej) |
| `getAdditionalProducts` / `getCrossell` | POST | **nie udało się** odgadnąć payloadu |
| `getOfferPromotion` | POST | `success:false` bez treści błędu |
| `getCalculatorOfferVariants/` (bez id) | POST | 404 — brakujący segment ścieżki |
| `getOfferAvailability/` (bez id) | POST | 404 — jw. |

Oferta użyta jako główny obiekt badawczy (z `data/offers.db`):
**Elios**, Taormina/Sycylia, `offerId=1091524`, wylot 2026-09-23, 5 nocy,
BB, WAW, cena listingowa **2047 zł/os**, biuro `ONHO` (Onholidays).

### 1. `POST /v2/api/getOfferDataFromMerlin` — bez ceny

Payload z zadania jest poprawny i przechodzi:

```json
{"query": {"service": [3], "type": [1], "departure": [278],
           "departureDate": "2026-09-23", "duration": {"min": 5, "max": 5},
           "rooms": {"adult": 2, "kid": 0, "ages": [], "inf": null}},
 "offerId": 1091524, "brand": "WAK"}
```

Odpowiedź (HTTP 200, `success: true`, 959 B) zawiera **wyłącznie** metadane hotelu:

```json
{"data": {"promoFirstMinute": true, "promoLastMinute": true, "objType": "H",
          "roundTripUrlName": null, "preSale": false, "isDedicated": false,
          "tagSportsTourism": false, "promoTop10": false,
          "attributes": [{"all": [{"26": "Bezpośrednio przy plaży"},
                                  {"4": "Życie nocne"}, {"13": "Nurkowanie"}]},
                         {"hotel": [...]}, {"freeTime": [...]}]}}
```

Ani jednego pola cenowego. Nazwa myli — „Merlin" to system rezerwacyjny,
ale ten endpoint oddaje z niego tylko atrybuty obiektu. **Do weryfikacji
ceny się nie nadaje.**

### 2. Jak znaleziono właściwy endpoint

Strona oferty (`/oferty/elios-1091524.html?…`) jest Next.js, chunki leżą pod
`/v2/_next/static/chunks/`. W `chunks/527-*.js` znalazł się builder payloadu,
a w `chunks/_app-*.js` — realne wywołania. Kluczowy fragment (odminifikowany):

```js
// chunks/_app-*.js — konfigurator pokoi
j = {adults, kids, serviceId, infants, duration, kidsAges,
     departureDate, transportId, departureCityId, departureCityCode,
     hotelId, tourOp, tourId, cruiseId, roundTripId,
     isAlternativeRoom: false, isOffer77: ...};
await this.api.post("/getCalculatorOfferVariants/".concat(offerId), j);
```

Dwie rzeczy, które wywracały wcześniejsze próby:

1. **`offerId` idzie w ŚCIEŻCE**, nie w ciele. Zapis z fazy 0
   (`/getCalculatorOfferVariants/`) był niekompletny — stąd 404.
2. **Ciało jest PŁASKIE.** Żadnego `{query: {...}}` znanego z `search.tripsSearch`.

### 3. `GET /v2/api/getInitOfferData/{offerId}/` — parametry + świeża cena

```http
GET /v2/api/getInitOfferData/1091524/?duration=5&startsAt=2026-09-23&departureId=278
```

Zwraca komplet potrzebny kalkulatorowi jednym strzałem (fragment):

```json
{"offerId": 1091524, "codeWak": 1091524, "hotelId": 31638,
 "serviceType": "BB", "serviceTypeId": 3, "tourId": 11194,
 "tourOpCode": "ONHO", "tourOperatorName": "Onholidays", "transportId": 1,
 "currentDuration": 5, "nights": 5, "departure": "WAW",
 "departureDate": "2026-09-23", "returnDate": "2026-09-28",
 "price": 2047, "cruiseId": 0, "roundTripId": 0, "scoreAvg": 8.4}
```

Dwie role: (a) dostarcza `tourOpCode`/`tourId`/`hotelId`/`serviceTypeId`,
których w tabeli `offer` nie mamy, (b) daje **świeżą** cenę „od", dzięki
czemu odróżniamy „listing kłamie" od „nasz snapshot się zestarzał".

Dla nieistniejącej oferty: HTTP **200** z `{"success": false, "data": null,
"errors": "⛔️ undefined"}` — nie 404, więc trzeba patrzeć na `success`.

`getOfferInfoBox` (POST, payload jak w pkt 1) zwraca te same dane plus
`offerHashId`, `objCode`, `objXCode`, opis HTML i atrybuty. Jest bogatszy,
ale wymaga POST-a z pełnym `query`; do naszego zastosowania `getInitOfferData`
wystarcza i jest tańszy.

### 4. `POST /v2/api/getCalculatorOfferVariants/{offerId}` — cena końcowa

Payload (wartości z `getInitOfferData`; `departureCityId` z `DEPARTURES`):

```json
{"adults": 2, "kids": 0, "serviceId": 3, "infants": 0, "duration": 5,
 "kidsAges": [], "departureDate": "2026-09-23", "transportId": 1,
 "departureCityId": 278, "departureCityCode": "WAW", "hotelId": 31638,
 "tourOp": "ONHO", "tourId": 11194, "cruiseId": null, "roundTripId": null,
 "isAlternativeRoom": false, "isOffer77": false}
```

Odpowiedź — lista realnych, rezerwowalnych wariantów pokoi:

```json
{"data": {"offers": [
  {"id": "pi-UZ6YrG1KiAk0…", "uid": "bd5efdd701fe…",
   "roomDesc": "Dbl sea view standard sea view",
   "tourOp": "ONHO", "serviceId": 3, "serviceDesc": "Śniadania (BB)",
   "transportId": 1, "duration": 5, "departureCode": "WAW",
   "basePrice": 4094, "totalPrice": 4094, "priceCurrency": "PLN",
   "roomCode": "31638", "searchRoomCode": "10950", "objectId": 31638,
   "roomDescAdditional": ["Śniadania", "Widok na morze"],
   "isLuggageIncluded": null,
   "departStart": {"airportCode": "WAW", "date": "2026-09-23", "time": "21:20"},
   "departEnd":   {"airportCode": "CTA", "date": "2026-09-23", "time": "00:10"},
   "returnStart": {"airportCode": "CTA", "date": "2026-09-28", "time": "16:55"},
   "returnEnd":   {"airportCode": "WAW", "date": "2026-09-28", "time": "19:50"}},
  {"roomDesc": "Superior/deluxe room superior sea view",
   "basePrice": 4498, "totalPrice": 4498,
   "roomDescAdditional": ["Śniadania", "Superior", "De lux", "Widok na morze"],
   "isLuggageIncluded": false}]},
 "sectionHeading": {...}, "showMoreRoomsButton": {...}}
```

Bonus: pełne godziny lotów tam i z powrotem — dane, których listing nie daje.

Gdy dla podanych parametrów nie ma wolnych miejsc, odpowiedź to
`success: true` z `{"offers": []}` (sprawdzone na wymyślonej dacie
2027-01-05). To informacja („wyprzedane"), nie błąd techniczny.

## Ustalenie krytyczne: `totalPrice` jest ZA CAŁY POKÓJ

Ten sam test, który README nakazuje robić każdej nowej integracji — zmiana
liczby osób przy stałej ofercie (Elios 1091524):

| dorosłych | `totalPrice` | / os. | wariant |
| --- | --- | --- | --- |
| 1 | 2550 | 2550 | Single superior sea view single use |
| 2 | 4094 | **2047** | Dbl sea view standard sea view |
| 3 | 5830 | 1943 | Dbl sea view standard sea view |

Cena rośnie liniowo z obłożeniem → to cena za komplet osób, nie za osobę.
Potwierdza to niezależnie `getOfferAvailability`, które mówi to wprost:

```http
POST /v2/api/getOfferAvailability/ONHO/{room.id}    (ciało = ten sam payload kalkulatora)
{"data": {"amountLeft": -1, "isPerPerson": false, "priceTotal": 4094,
          "pricePerson": 0, "priceCurrency": "PLN", "promisePrice": 90,
          "isPromisePriceShown": true, "isAvailable": true, "earlyBooking": false}}
```

**Dlatego `verify.parse_variants` dzieli `totalPrice` przez liczbę osób.**
Gdyby tego nie robić, każda oferta raportowałaby „zawyżona o 100%" —
fałszywy alarm wbudowany na stałe w narzędzie.

Uwaga na pułapkę: dla 1 osoby serwis podstawia *inny pokój* (single use),
a dla 3 osób cena/os spada, bo trzecia osoba dostaje dostawkę. Weryfikacja
musi więc pytać o dokładnie tyle osób, ile ma profil (`profile.adults`).

## Wynik: cena z listingu jest prawdziwa

7 losowych ofert z `data/offers.db`, dla każdej `getInitOfferData` +
`getCalculatorOfferVariants`, porównanie najtańszego wariantu / 2 osoby
z ceną ze snapshotu:

| hotel | termin | listing | min/os | różnica | max/os |
| --- | --- | --- | --- | --- | --- |
| BV Kalafiorita Resort | 2026-09-23, 5n FB | 2923 | 2923 | **+0.0%** | 2923 |
| Tropic Relax | 2026-09-24, 6n BB | 2993 | 2993 | **+0.0%** | 3417 |
| Asteria Family Resort Side | 2026-09-25, 5n AI | 2851 | 2851 | **+0.0%** | 4383 |
| Fame | 2026-09-19, 7n AI | 3029 | 3029 | **+0.0%** | 3029 |
| Alkyonides (Kremasti) | 2026-09-21, 7n BB | 2902 | 2902 | **+0.0%** | 3162 |
| Sun Beach (Platamonas) | 2026-09-23, 7n HB | 2949 | 2949 | **+0.0%** | 2949 |
| Pineta Petto Bianco | 2026-09-25, 5n HB | 2359 | 2359 | **+0.0%** | 2539 |

Zgodność co do złotówki, 7/7. Powtórzone potem przez `hs weryfikuj
wrzesien-okazje --top 8` na innej ósemce — 0.0% w 7 przypadkach, ósmy to
Elios opisany niżej (wyprzedany tańszy pokój, nie cena-wabik).
Łącznie **14 z 15 ofert zgodnych co do złotówki**.

**Wniosek: ranking cenowy stoi na twardym gruncie.** Cena „od" nie jest
wabikiem — jest ceną najtańszego pokoju, który da się kupić.

### Ale „od" nadal znaczy „od"

Kolumna `max/os` pokazuje drugą stronę: Asteria Family Resort Side ma
najtańszy pokój za 2851 zł/os, a najdroższy za **4383 zł/os (+54%)**.
Cena z listingu jest prawdziwa, ale dotyczy najsłabszego pokoju w hotelu.
Dlatego `Verification` raportuje `max_price`, a `hs weryfikuj` dopisuje
w uwagach „lepszy pokój do +N%” i podsumowuje, ile ofert ma rozrzut >15%.

### Jedyny zaobserwowany rozjazd — i to nie ten, którego się baliśmy

Elios wyprzedał się nam pod ręką w trakcie prac. Przebieg zdarzeń, minuta
po minucie (wszystko 2026-08-31):

| godz. | `getInitOfferData.price` | warianty kalkulatora | komentarz |
| --- | --- | --- | --- |
| 15:34 | 2047 | 4094 (Dbl) **i** 4498 (Superior) | stan wyjściowy, zgodność 0.0% |
| 15:41 | 2047 | **tylko** 4498 → 2249/os | tańszy pokój zniknął, listing jeszcze go pokazuje |
| 15:52 | **2249** | tylko 4498 → 2249/os | listing dogonił kalkulator |

Wniosek: cena „od" nie kłamie, ale **potrafi się spóźnić o kilkanaście
minut** za wyprzedaniem najtańszego pokoju. To okno jest wąskie, jednak
istnieje — i jest jedynym przypadkiem, w którym listing pokazał cenę
niższą niż faktycznie dostępna.

Rozróżniamy więc dwie sytuacje, które gołym okiem wyglądają tak samo:

* **`odchylenie` / `zawyzona`** — świeża cena „od" też odstaje od kalkulatora
  (okno z wiersza 15:41). Tu winne jest źródło.
* **`nieaktualna`** — świeża cena „od" **zgadza się** z kalkulatorem, ale
  różni się od naszego snapshotu (wiersz 15:52). Tu winna jest nasza baza.

Wrzucenie tego drugiego do „cena zawyżona" oskarżałoby serwis o nasz własny
problem i zafałszowałoby odpowiedź na pytanie, po co ten moduł powstał.

## Czego nie udało się ustalić

### `getCrossell` / `getAdditionalProducts` (bagaż, transfer, parking)

Z `chunks/_app-*.js` wyczytano kształt ciała:

```js
w = {airport: [r], startsAt, endsAt, priceFactor, country, value, currency, providerCode};
await d.getCrossSells(w, m);   // -> api.post("/getCrossell", w)
```

Próba z `{"airport": ["WAW"], "startsAt": "2026-09-23", "endsAt": "2026-09-28",
"priceFactor": 4094, "country": "IT", "value": "WAW", "currency": "PLN",
"providerCode": "ONHO"}` zwraca HTTP 200 z `{"success": false, "data": null}`
— **bez treści błędu**, więc nie ma czego debugować po omacku. Podejrzenie:
`value` to wewnętrzne id lotniska (nie kod IATA), a `country` może wymagać
innego słownika.

`getAdditionalProducts` jest o krok dalej w łańcuchu — jego payload powstaje
z wyniku `getCrossell` (`getProductsMap(crossSells, …)`), więc bez tamtego
nie da się go zawołać. Bez payloadu: `{"success": false, "error":
{"message": "[GET .../v1/additional-product/list] Request failed with
status code 400", "status": 400}}`.

**Dlaczego to nie blokuje.** To są dodatki **opcjonalne** (bagaż rejestrowany,
transfer, parking, ubezpieczenie „Cena Gwarantowana" = `promisePrice: 90`),
a nie obowiązkowe składowe ceny. Cena obowiązkowa to `totalPrice` wariantu.
Jedyny sygnał o bagażu, jaki mamy, to `isLuggageIncluded` przy wariancie
(`true` / `false` / `null` = biuro nie podało) — i tyle raportujemy.

Nie znaleziono też żadnego śladu obowiązkowej dopłaty typu TFG/TFP
doliczanej poza `totalPrice`.

### `offerConfiguratorV2/offerVariants`

Działa, ale to inny model danych — służy do szukania **alternatywnych
terminów** tej samej oferty, nie do wyceny wybranego. Payload
(z `chunks/527-*.js`):

```js
{offerId, departureDate: [], duration: [], adults, kidsAges, service: [],
 transportType: [], departurePlace: [], providerIds: [],
 limit, orderBy, totalPrice: true}
```

Bez `providerIds` (choćby pustej tablicy) odpowiada
`{"success": false, "errors": ["providerIds is a required field"]}` —
to właśnie ten błąd zobaczyliśmy przy pierwszej próbie z payloadem
`{query, offerId, brand}`. Może się przydać do przyszłego kalendarza cen;
do weryfikacji ceny konkretnej oferty kalkulator jest właściwszy.

## Etykieta

Rekonesans zmieścił się w ~31 zapytaniach do API (przy limicie ~40),
każde z odstępem ≥1.5 s. Chunki JS to statyczne pliki z CDN, pobrane raz.
`PriceVerifier` trzyma `delay ≥ 1.5 s` między wywołaniami i ponawia
**tylko** błędy sieci (2×, backoff 3 s / 6 s). Odpowiedź `success: false`
nie jest ponawiana — to trwały stan po stronie serwisu, a nie usterka łącza;
dobijanie go niczego by nie zmieniło poza obciążeniem cudzego serwera.

## Użycie

```bash
PYTHONPATH=src python3 -m holiday_searcher.cli weryfikuj wrzesien-okazje --top 8
PYTHONPATH=src python3 -m holiday_searcher.cli weryfikuj wrzesien-okazje --top 20 --delay 2
```

Bierze N najtańszych ofert wakacje.pl danego profilu (`offer` + najnowszy
`price_snapshot`, profil wiązany przez `run.profile` → `price_snapshot.run_id`),
weryfikuje każdą i zapisuje wynik do tabeli `price_verification`
(append-only, jak `price_snapshot` — dzięki temu widać historię weryfikacji):

```sql
CREATE TABLE price_verification (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key     TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    listing_price INTEGER,
    final_price   INTEGER,
    diff_pct      REAL,
    details_json  TEXT      -- warianty pokoi, świeża cena „od", werdykt, uwagi
);
```

Kolory różnicy: zielony ≤2%, żółty 2–10%, czerwony >10%.
Oferty, których nie dało się zweryfikować (brak `offerId` w URL, oferta
wycofana, termin wyprzedany, błąd sieci), trafiają do tabeli z pustą ceną
końcową i powodem w `details_json.error` — nigdy nie przerywają przebiegu.
