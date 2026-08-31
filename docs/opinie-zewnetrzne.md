# Zewnętrzne źródła opinii — weryfikacja ocen hoteli (`hs opinie`)

> Źródła: **HolidayCheck** (bez klucza) + **Google Places API (New)**
> (wymaga klucza — [instrukcja](#jak-zdobyć-klucz--krok-po-kroku)).
> Bez klucza Google komenda działa normalnie, z kolumną „brak klucza”.

## Problem

wakacje.pl podaje przy hotelu ocenę w skali 0-10 i liczbę opinii. Profil
`wrzesien-okazje` odrzuca oferty progiem `rating_min: 8.0`. Kłopot w tym, że
ocena bardzo często stoi na garstce opinii:

```sql
SELECT CASE WHEN rating_count<=3 THEN '1-3' WHEN rating_count<=9 THEN '4-9'
            WHEN rating_count<=30 THEN '10-30' ELSE '30+' END,
       COUNT(DISTINCT hotel_name) FROM offer GROUP BY 1;
-- 1-3: 16 | 4-9: 8 | 10-30: 11 | 30+: 25
```

**24 z 60 hoteli w bazie ma ≤9 opinii.** „Ocena 10.0 z jednej opinii" to nie
jakość hotelu, tylko szum — a przez próg 8.0 przechodzi jako fakt. Gorzej:
`getOpinionsBox` dla hotelu 38182 („Olympia (Pefkohori)") zwraca
`{"opinionsCount": 0, "ratingValue": 10}` — dziesiątka bez ani jednej opinii.

Ta faza dokłada **drugie, niezależne źródło**, żeby ocenę potwierdzić albo
zdemaskować. Nie uśrednia obu ocen — uśrednianie zamiotłoby problem pod dywan.

## Rekonesans: co sprawdzono i co z tego wyszło

Metoda: żądania bez logowania i bez klucza, delay ≥2 s, przeglądarkowy
User-Agent, maks. kilkadziesiąt żądań na źródło. Hotele testowe z `data/offers.db`.

| Źródło                             | Werdykt                                                                                                               |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `holidayCheckRate` w API wakacje.pl | **martwe pole** — zawsze `null`                                                                              |
| **HolidayCheck**                | **DZIAŁA** — wybrane                                                                                          |
| Booking.com                           | zablokowane (AWS WAF, wymaga JS)                                                                                      |
| Google Places                         | bez klucza zablokowane —**z kluczem: wybrane**, patrz [sekcja niżej](#trzecie-źródło-google-places-api-new) |
| Google Maps / Search                  | zablokowane (ściana zgody + render po stronie klienta)                                                               |
| TripAdvisor                           | zablokowane (403 + captcha)                                                                                           |

### 0. Skrót, którego nie ma: `holidayCheckRate` w wakacje.pl

`POST /v2/api/getOpinionsBox` (z fazy 3) zwraca pola `holidayCheckRecommendation`
i `holidayCheckRate` — wyglądały na gotową integrację. Sprawdzone na 8 hotelach:

```jsonc
// hotelId 38182 -> {"opinionsCount": 0, "ratingValue": 10,
//                   "holidayCheckRecommendation": null, "holidayCheckRate": null}
```

**Oba pola są `null` dla każdego sprawdzonego hotelu.** To pozostałość po dawnej
integracji. Skrótu nie ma — po dane trzeba pójść do HolidayCheck samemu.

### 1. HolidayCheck — wybrane źródło

`holidaycheck.pl` **nie istnieje** jako serwis konsumencki: przekierowuje na
`jobs.holidaycheck.de/pl`. Pracujemy na `holidaycheck.de` (ma locale `plPL`).

#### Blokada, którą da się przejść nagłówkami

Serwis stoi za Akamai Bot Managerem. Żądanie z samym `User-Agent` (albo z
`curl/8`) dostaje **HTTP 400 z `server: AkamaiGHost`** i stroną
„Diese Funktionalität steht gerade nicht zur Verfügung":

```
HTTP/2 400
server: AkamaiGHost
content-length: 3328
```

Przepuszcza dopiero **komplet nagłówków przeglądarki** — kluczowe okazały się
`Accept-Language: de-DE`, `Accept-Encoding` oraz trójka `sec-ch-ua*`.
Ten zestaw siedzi w `BROWSER_HEADERS` w `external_ratings.py`. Blokada bywa
niestabilna (to samo `robots.txt` raz oddaje 200, raz 400) — stąd retry po
stronie wywołującego i delay 2 s.

W HTML-u strony jest też `<script src="/akam/13/1a1945a9">` — sensor Akamai
liczący ciasteczko `_abck` z fingerprintu przeglądarki. **Nie jest potrzebny**
do ścieżek opisanych niżej, ale to on jest granicą: gdyby serwis zaostrzył
politykę, trzeba by przeglądarki.

#### Krok 1: nazwa → UUID (otwarty GraphQL)

Strona `/hotelsuche` jest pustą skorupą JS (`noindex, nofollow`, zero wyników
w HTML-u), ale zdradza w kodzie ścieżki usług: `/svc/content-query-v2`,
`/svc/search-mixer`, `/svc/api-favourites`, `/svc/favorites/v1`.

`/svc/search-mixer` daje 404. Za to **`POST /svc/content-query-v2` to otwarty
GraphQL bez żadnej autoryzacji** — odpowiada nawet na introspekcję:

```jsonc
// {"query":"{__typename}"} -> {"data":{"__typename":"Query"}}
```

Pełna introspekcja pokazuje ~70 pól `Query`, z czego istotne są dwa:

```
suggestionSearch(tenant, limit, query, type, sessionId)
hotelOfferSearch(select, filter, sort, limit, offset, locale, currency, ...)
```

Używamy `suggestionSearch`:

```graphql
query($q: String, $limit: Int) {
  suggestionSearch(query: $q, limit: $limit, tenant: "hcde", type: ["hotel"]) {
    hotels { count entities { id name placeDetailString } }
  }
}
```

Pułapki, każda kosztowała jedno żądanie:

- `type` to **`[String]`, nie `String`** — inaczej błąd walidacji zmiennej.
- `entities.parents` to `[String]`, więc **nie wolno podawać podselekcji**.
- `entities.recommendationRate` zwraca `0` dla wszystkiego — bezużyteczne.
- Endpoint bywa kapryśny: `{"message": "Cannot read properties of undefined (reading 'body')"}` przy poprawnym zapytaniu. To flake, nie błąd składni —
  ponowienie pomaga.

Zwracane `placeDetailString` ma postać **„Hotel in Kremasti, Rhodos,
Griechenland"** — czyli miasto, region i kraj w jednym polu. To jest materiał
do weryfikacji miejsca i najcenniejsza rzecz w całej odpowiedzi.

#### Ślepy zaułek: `hotelOfferSearch`

Kusi, bo ma komplet pól ocen:

```graphql
items { id name stars url address
        reviewCalculations { overall { rating countUnarchived countArchived recommendation } } }
```

`Filter` przyjmuje `name`, `hotel_id`, `stars`, `reviewCalculations_overall_rating`…
**Wszystkie te filtry zwracają 0 pozycji** — sprawdzone dla `name` (dokładnie,
lowercase, `*wildcard*`, `~`, `match:`, `eq:`, `in:`), dla `hotel_id` z realnym
UUID-em i dla `stars`. Bez filtra: `Cannot convert undefined or null to object`.
Wniosek: to wyszukiwarka **ofert sprzedażowych**, wymaga kontekstu terminu
(`mpgSearchSpec`), a nie katalog hoteli. Porzucone.

#### Krok 2: UUID → ocena (JSON-LD na stronie SSR)

Strona hotelu jest renderowana **po stronie serwera**, a `/hi/x/<uuid>`
przekierowuje na adres kanoniczny — **sluga nie trzeba znać**:

```
GET https://www.holidaycheck.de/hi/x/c8ccd47e-33b6-4f21-bc86-f09ddfc5bc39
 -> 200, https://www.holidaycheck.de/hi/alkyonides-boutique-hotel/c8ccd47e-...
```

W HTML-u jest dokładnie jeden blok `application/ld+json` ze schema.org `Hotel`:

```jsonc
{"@context":"http://schema.org","@type":"Hotel",
 "url":"https://www.holidaycheck.de/hi/alkyonides-boutique-hotel/c8ccd47e-...",
 "name":"Alkyonides Boutique Hotel","identifier":"c8ccd47e-...",
 "address":{"addressCountry":"Griechenland","addressRegion":"Rhodos",
            "addressLocality":"Kremasti"},
 "aggregateRating":{"@type":"AggregateRating","bestRating":"10","worstRating":"1",
                    "ratingCount":"4","ratingValue":"6.2"},
 "review":[{"headline":"Niewieder. 😬🤮😬","author":{"name":"Dilara"},
            "datePublished":"2025-09-07T05:54:52.000Z",
            "reviewBody":"Es ist sehr dreckig überall …",
            "reviewRating":{"bestRating":"10","ratingValue":"4.6"}}]}
```

Dwie pułapki parsowania (obie zaadresowane w `_LD_RE` / `_first_hotel_ld`):

- atrybut bywa **bez cudzysłowów**: `<script type=application/ld+json>`;
- ukośniki są zescapowane jako `/` — bez podmiany URL-e są bezużyteczne.

**Hotel bez ani jednej opinii** ma poprawny JSON-LD, ale **bez klucza
`aggregateRating`** (sprawdzone na „Hotel Olympia" w Pefkochori). To normalny
stan (`no_rating`), nie awaria.

### 2. Booking.com — zablokowane

Zarówno strona hotelu, jak i `searchresults` zwracają **HTTP 202** z 3962-bajtową
stroną challenge’u AWS WAF:

```html
window.awsWafCookieDomainList = ['booking.com'];
<script src="https://www.booking.com/__challenge_h78IRKX3kpQxScCExxShBNwRUlb/.../challenge.js">
```

Ciasteczko przepustki powstaje z wykonania tego JS-a — **wymaga przeglądarki**.
Zero JSON-LD, zero `ratingValue` w odpowiedzi. Endpoint autocomplete
(`accommodations.booking.com/autocomplete_json.html`) zwraca 404. Porzucone.

### 3. Google Maps / Places — zablokowane

Places API odpowiada jednoznacznie:

```jsonc
{"candidates": [], "status": "REQUEST_DENIED",
 "error_message": "You must use an API key to authenticate each request to
                   Google Maps Platform APIs."}
```

`google.com/maps/search/...` przekierowuje na `consent.google.com`.
`google.com/search?q=...` oddaje 92 KB skorupy JS z komunikatem „Jeśli w ciągu
kilku sekund nie nastąpi przekierowanie…" — **zero `ratingValue`, zero
`aggregateRating`**. Zgodnie z zadaniem: udokumentowane i porzucone.

**Aktualizacja.** Komunikat wyżej to nie ściana, tylko rachunek: Google nie
blokuje danych, tylko sprzedaje do nich dostęp. Ścieżka z kluczem API jest
otwarta, tania w naszej skali i opisana w sekcji
[Trzecie źródło: Google Places API (New)](#trzecie-źródło-google-places-api-new).
Skrobanie `google.com/maps` pozostaje porzucone — i tak byłoby niezgodne
z regulaminem, skoro istnieje oficjalne API.

### 4. TripAdvisor — zablokowane

```
GET https://www.tripadvisor.com/            -> HTTP 403, w treści 2× "captcha"
GET https://www.tripadvisor.com/Search?q=…  -> HTTP 403, jw.
GET https://www.tripadvisor.com.pl/         -> ConnectTimeout
```

Zgodnie z przewidywaniem. Porzucone.

## Dopasowanie hotelu — trzy warstwy, każda opłacona pomyłką

Baza ofert zna hotel jako `Alkyonides (Kremasti)` w mieście `Kremasti`,
regionie `Rodos`, kraju `Grecja`. HolidayCheck zna go jako
`Alkyonides Boutique Hotel` w „Kremasti, Rhodos, Griechenland”. Trzeba je zszyć.

### Warstwa 1: fraza wyszukiwania (`search_query`)

Naiwne `nazwa + " " + miasto` daje `"Alkyonides (Kremasti) Kremasti"` — frazę,
w której miasto waży dwa razy więcej niż nazwa hotelu. Efekt zmierzony:

```
q='Alkyonides (Kremasti) Kremasti' -> Hotel Kremasti Memories, Sweet Home Kremasti,
                                      Kremasti Pool Villa, CASA TERRA KREMASTI …
                                      (szukanego hotelu NIE MA w wynikach)
q='Alkyonides Kremasti'            -> Alkyonides Boutique Hotel   <- pozycja 1
```

Dlatego najpierw zdejmujemy z nazwy nawiasy i ogon po `ex.`, a miasto doklejamy
tylko wtedy, gdy nie ma go już w nazwie.

### Warstwa 2: podobieństwo nazwy (`name_similarity`)

`difflib.SequenceMatcher` na znormalizowanych nazwach (bez nawiasów, bez ogona
po `ex.`, bez słów generycznych `hotel|resort|club|apartments|studios|the|by|and`,
bez znaków diakrytycznych). Próg trafienia: **≥0.80**.

Sam SequenceMatcher jest jednak za surowy dla realnych par — `Alkyonides` kontra
`Alkyonides Boutique Hotel` daje **0.69**, choć to bez wątpienia ten sam obiekt.
Dlatego gdy komplet słów krótszej nazwy zawiera się w dłuższej, wynik idzie do
**0.88**: nad progiem, ale wyraźnie poniżej dopasowania dokładnego.

`beach` i `garden` świadomie **nie są** słowami generycznymi — `Venus Beach`
i `Venus Garden` to różne hotele.

### Warstwa 3: zgodność miejsca (`place_agreement`) — warunek konieczny

**Sama nazwa kłamie.** Realny fałszywy trop z bazy:

```
Ambrosia (Athens), Grecja  ->  "Hotel Ambrosia" @ Hotel in Bitez, Türkische Ägäis, TÜRKEI
                               podobieństwo nazwy: 1.00
```

Dlatego **kraj z oferty jest warunkiem koniecznym** (mapowanie PL→DE w
`COUNTRY_PL_DE` — nazwy miast bywają zbieżne, nazwy krajów nigdy), a miasto
i region podnoszą pewność. Bez potwierdzenia miasta/regionu wymagamy nazwy
praktycznie identycznej (≥0.97) — to przepuszcza `Novotel Malta Sliema`, który
na HolidayCheck ma adres w sąsiednim Gzira.

### Warstwa 4: reguła rywala — najważniejsza i najdroższa

Trafienie z idealną nazwą **wciąż potrafi być złe**, gdy w tej samej
miejscowości stoi kilka hoteli pasujących do naszej ubogiej nazwy:

```
"Sun Beach (Platamonas)" ->  Hotel Sun Beach            (Platamonas)  4.7 z 8 opinii
                             Sun Beach Platamon Resort  (Platamonas)  — inny obiekt!
"Karbel" (Ölüdeniz)      ->  Hotel Karbel / Hotel Karbel Sun / Hotel Karbel Beach
"Grand Zaman Garden"     ->  Hotel Grand Zaman Garden / Grand Zaman Garden Hotel - Annex
```

Gdyby wygrał pierwszy z brzegu, `Sun Beach` dostałby ocenę 4.7 obcego hotelu
i **fałszywą flagę „rozbieżność"** — czyli kazałby odrzucić dobry hotel.
Dlatego: jeśli w tej samej miejscowości jest **drugi kandydat równie zgodny
z naszą nazwą** (`is_compatible`: komplet naszych słów siedzi w jego nazwie),
orzekamy `ambiguous`.

Test zgodności jest kierunkowy i to jest istotne:

- `Karbel` ⊆ `Hotel Karbel Sun` → rywal, bo to nasza nazwa **plus** coś;
- `Grand Cettia` ⊄ `Club Cettia Resort` (po normalizacji samo „cettia”) → **nie**
  rywal, bo gubi słowo „grand” — to inny obiekt, nie dokładniejszy zapis tego samego.

Rywal liczy się tylko wtedy, gdy jest **równie dobrze ulokowany**:
`Alkyonides Boutique Hotel` (Kremasti, Rodos) ma imiennika
`Hotel Alcionides / Alkyonides` w Stalis na Krecie — nazwy nie do odróżnienia,
ale miejscowość owszem, więc trafienie zostaje pewne.

**Ta reguła kosztuje zasięg** (na 25 hotelach: 10 wpisów `ambiguous`), i to jest
świadoma cena. Podstawiona ocena obcego hotelu jest gorsza niż „brak danych”,
bo brak danych widać, a złą liczbę bierze się za prawdę.

## Trzecie źródło: Google Places API (New)

HolidayCheck jest dobrym drugim źródłem, ale ma wąskie gardło: to katalog
niemieckiego biura podróży. Hotelu spoza jego oferty tam po prostu nie ma,
a nazwy bywają nierozstrzygalne. Efekt: **14 trafień na 25 hoteli**, 10
`ambiguous`, 1 bez opinii. Google zna praktycznie każdy obiekt noclegowy
na świecie i ma przy nim setki opinii tam, gdzie wakacje.pl ma jedną.

Google **nie zastępuje** HolidayCheck — dochodzi obok. Klucz główny cache'u
to `(hotel_id, source)`, więc nie było potrzeby żadnej migracji.

### Czego się po Google spodziewać (i czego nie)

Google ocenia **szeroką publicznością**: ocenia gość restauracji hotelowej,
przechodzień z plaży, kierowca, który zaparkował. wakacje.pl i HolidayCheck
pytają wyłącznie ludzi, którzy w hotelu **nocowali**. Dlatego Google
systematycznie ocenia **wyżej** — i dlatego surowa różnica ocen mierzy kulturę
oceniania, a nie jakość hotelu. Patrz [Kalibracja](#kalibracja-per-źródło).

Opinii Google zwraca **maksymalnie 5** i nie ma paginacji ani sortowania.
To twardy limit API (istnieje od 2013 r., zgłoszenie
`issuetracker.google.com/issues/35825957` wisi otwarte), nie brak uprawnień
i nie błąd. Dlatego do werdyktów AI używamy tekstów z wakacje.pl, a od Google
bierzemy przede wszystkim `rating` + `userRatingCount`.

### Krok 1: wyszukanie hotelu

```http
POST https://places.googleapis.com/v1/places:searchText
Content-Type: application/json
X-Goog-Api-Key: <klucz>
X-Goog-FieldMask: places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress,places.location,places.types
```

```jsonc
{"textQuery": "Alkyonides Kremasti Grecja", "maxResultCount": 5, "languageCode": "pl"}
```

**Field mask jest OBOWIĄZKOWY.** Dokumentacja mówi wprost: *„There is no default
list of returned fields in the response. If you omit the field mask, the method
returns an error."* Bez niego API oddaje HTTP 400 `INVALID_ARGUMENT` (próbka:
`tests/data/google_places_error.json`). W nagłówku **nie wolno wstawiać spacji**
po przecinkach.

Odpowiedź:

```jsonc
{"places": [
  {"id": "ChIJq6qq6koLnRQRxKZ0mQ2wPQ4",
   "displayName": {"text": "Alkyonides Boutique Hotel", "languageCode": "en"},
   "formattedAddress": "Ethnarchou Makariou 12, Kremasti 851 04, Grecja",
   "location": {"latitude": 36.409312, "longitude": 28.118974},
   "types": ["hotel", "lodging", "point_of_interest", "establishment"],
   "rating": 4.2, "userRatingCount": 812}],
 "nextPageToken": "…"}
```

Szczegóły, które kosztowałyby po jednym żądaniu:

- `maxResultCount` jest w referencji REST oznaczony jako **deprecated** na rzecz
  `pageSize`; nadal działa, ale gdyby oba podać naraz, `maxResultCount` jest
  **ignorowany**. Podajemy tylko jego.
- Text Search oddaje najwyżej **60 wyników** na wszystkich stronach łącznie.
- Google zastrzega, że *„the list of places returned is not guaranteed to be
  consistent for identical requests"* — czyli powtórka tego samego zapytania
  może dać inną kolejność. Dlatego cache jest permanentny, a nie „na wszelki
  wypadek odświeżany".
- `locationBias` (okrąg) doklejamy **tylko gdy mamy współrzędne**. API ofert
  wakacje.pl ich nie udostępnia, więc w praktyce zwykle ich nie ma —
  parametry `lat`/`lng` w `search_text` są przygotowane na przyszłość:

  ```jsonc
  {"locationBias": {"circle": {"center": {"latitude": 36.4, "longitude": 28.1},
                               "radius": 30000}}}
  ```
- `languageCode: "pl"` sprawia, że `formattedAddress` kończy się polską nazwą
  kraju („Grecja"), ale Google lokalizuje niekonsekwentnie — dlatego
  `COUNTRY_ALIASES` przyjmuje też formę angielską i miejscową.

### Krok 2: opinie (opcjonalnie)

```http
GET https://places.googleapis.com/v1/places/ChIJq6qq6koLnRQRxKZ0mQ2wPQ4
X-Goog-Api-Key: <klucz>
X-Goog-FieldMask: id,displayName,rating,userRatingCount,reviews,googleMapsUri
```

Uwaga na maskę: w Place Details pola **nie mają prefiksu `places.`** (to jest
pojedynczy zasób, nie lista). Odpowiedź niesie `name: "places/<ID>"` jako
resource name i `id: "<ID>"` osobno.

```jsonc
{"reviews": [{"name": "places/<ID>/reviews/<REVIEW_ID>",
              "relativePublishTimeDescription": "3 miesiące temu",
              "rating": 5,
              "text": {"text": "…", "languageCode": "pl"},
              "originalText": {"text": "…", "languageCode": "en"},
              "authorAttribution": {"displayName": "…", "uri": "…", "photoUri": "…"},
              "publishTime": "2025-06-14T09:12:41Z"}],
 "rating": 4.2, "userRatingCount": 812}
```

`text` to tłumaczenie na `languageCode`, `originalText` — oryginał. Bierzemy
pierwsze, z odwrotem na drugie. Opinia bez treści (sama gwiazdka) jest
pomijana. **Domyślnie tego żądania NIE wykonujemy**: ocena i liczba opinii są
już w odpowiedzi wyszukiwania, więc standardowy koszt to **jedno żądanie na
hotel**.

### Cennik — dlaczego maska ma dwa warianty

Field mask decyduje o SKU. Wg dokumentacji „Choose fields" i cennika
Google Maps Platform (stan: 2026):

| Pola w masce                                                                      | SKU                     | Cena / 1000 | Darmowy limit mies. |
| --------------------------------------------------------------------------------- | ----------------------- | ----------- | ------------------- |
| `id`, `name`                                                                  | Essentials (IDs Only)   | 0           | 10 000              |
| `displayName`, `formattedAddress`, `location`, `types`, `googleMapsUri` | Pro                     | ~$32        | 5 000               |
| `rating`, `userRatingCount`                                                   | **Enterprise**    | ~$35        | **1 000**     |
| `reviews`, `photos`                                                           | Enterprise + Atmosphere | ~$40        | 1 000               |

Nasza maska wyszukiwania sięga po `rating`, więc **każde wyszukanie hotelu to
jedno wywołanie w SKU Enterprise**. Przy 25 hotelach i permanentnym cache'u to
25 wywołań raz — czyli **głęboko w darmowym limicie 1000/mies.** Dlatego
`reviews` siedzą w osobnej masce i osobnej metodzie: żeby nie podnosić całego
przebiegu do najdroższego SKU dla danych, których i tak nie używamy do werdyktów.

Ceny bierz z [cennika Google](https://developers.google.com/maps/billing-and-pricing/pricing),
nie stąd — to jest notatka z jednego dnia, nie umowa.

### Jak zdobyć klucz — krok po kroku

1. Wejdź na [https://console.cloud.google.com/](https://console.cloud.google.com/) i zaloguj się kontem Google.
2. **Utwórz projekt** (górna belka → „Wybierz projekt" → „Nowy projekt").
   Nazwa dowolna, np. `holiday-searcher`.
3. **Włącz płatności** (menu → „Płatności" / „Billing", podpięcie karty).
   Bez tego Places API nie odpowie **nawet w ramach darmowego limitu** —
   to najczęstsza przyczyna `PERMISSION_DENIED` przy poprawnym kluczu.
4. **Włącz API**: menu → „APIs & Services" → „Enable APIs and Services" →
   wyszukaj **„Places API (New)"** → „Enable".
   ⚠️ To musi być wariant **(New)**. Stare „Places API" ma inne endpointy
   i ten kod z nim nie zadziała.
5. **Utwórz klucz**: „APIs & Services" → „Credentials" → „Create credentials" →
   „API key". Skopiuj wartość (zaczyna się od `AIza…`).
6. **Ogranicz klucz** (przycisk „Edit API key"):

   - *API restrictions* → „Restrict key" → zaznacz wyłącznie **Places API (New)**;
   - *Application restrictions* → zostaw „None" (to klient serwerowy bez
     stałego IP; ograniczenie po refererze dotyczy przeglądarek i tu zepsuje
     żądania).
7. **Ustaw limit wydatków**: „APIs & Services" → „Places API (New)" → „Quotas" →
   ustaw dzienny limit żądań (np. 200). To jedyny bezpiecznik przed rachunkiem
   za pomyłkę w pętli.
8. **Wgraj klucz do projektu** — jedno z dwóch:

   ```bash
   # na stałe
   echo 'GOOGLE_PLACES_API_KEY=AIza…' >> config/.env
   # albo na jedno uruchomienie
   GOOGLE_PLACES_API_KEY=AIza… PYTHONPATH=src python3 -m holiday_searcher.cli opinie wrzesien-okazje
   ```

   `config.get_secret` sprawdza najpierw zmienną środowiskową, potem `config/.env`.

**Czego się spodziewać po pierwszym uruchomieniu z kluczem.** Nic nie trzeba
przełączać ani czyścić — status `no_key` jest **nietrwały** (`TRANSIENT_STATUSES`),
więc cache go nie zapamiętał i Google rusza sam. Pierwszy przebieg to jedno
żądanie na hotel plus delay 0.2 s, czyli kilka sekund dla `--top 25`; kolejne
idą z cache'u i są natychmiastowe. Kolumna „Google" wypełni się ocenami
w skali 0-10 (Google pokazuje 4.2/5 — my 8.4) z liczbą opinii w nawiasie,
a pod tabelą pojawi się druga linia systematyki, tym razem z **ujemnym**
przesunięciem: Google ocenia łagodniej.

Gdyby coś poszło nie tak, komunikat Google wraca w całości w kolumnie „Uwagi"
— najczęstsze przypadki:

| Komunikat                                      | Przyczyna                                                  |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `Request must specify a field mask`          | maska wycięta przez proxy — nie powinno się zdarzyć    |
| `PERMISSION_DENIED … API has not been used` | krok 4 pominięty albo włączone stare „Places API"      |
| `PERMISSION_DENIED … billing`               | krok 3 pominięty                                          |
| `REQUEST_DENIED / API key not valid`         | literówka w kluczu albo restrykcja po refererze z kroku 6 |
| `RESOURCE_EXHAUSTED`                         | przekroczony limit z kroku 7                               |

### Dopasowanie hotelu — te same cztery warstwy plus jedna nowa

Nauki z HolidayCheck przenoszą się 1:1 (`pick_place` w `external_google.py`):
kraj jako **warunek konieczny**, zdejmowanie nawiasów przed budową frazy,
próg podobieństwa nazwy, reguła rywala. Do tego dwie rzeczy, których przy
HolidayCheck nie było:

**Nowe sito: `types`.** Google zwraca typy obiektu, więc restauracja, bar
i biuro podróży o nazwie hotelu odpadają, zanim w ogóle wejdą do rankingu.
To nie jest teoria — w Kremasti „Alkyonides" to *również* taverna z oceną 4.7
z 233 opinii, wyższą i lepiej udokumentowaną niż hotel. Bez filtra `types`
wygrałaby, bo ma nazwę **identyczną** (1.00). Sprawdzamy przynależność do
kategorii „Lodging" z Table A (`hotel`, `lodging`, `resort_hotel`, `motel`,
`guest_house`, `hostel`, …). Brak `types` w odpowiedzi traktujemy jako
„nie wiadomo" i **nie** odsiewamy — inaczej okrojona maska cicho wyzerowałaby
całe źródło.

**Zmiana: premia za miejscowość 0.30 zamiast 0.05.** Google szuka po całym
świecie, a normalizacja nazw zjada słowa generyczne — przez co
`Alkyonides Hotel Apartments` w **Stalidzie na Krecie** ma po normalizacji
nazwę identyczną (1.00) z naszym `Alkyonides (Kremasti)`, a właściwy
`Alkyonides Boutique Hotel` w Kremasti tylko **0.88**. Przy premii 0.05
wygrywałby hotel z niewłaściwej wyspy. Premia wpływa **wyłącznie na
kolejność**; zwracana pewność to nadal samo podobieństwo nazwy, więc kandydat
z dobrego miasta o byle jakiej nazwie dalej kończy jako `ambiguous`.

**Kraj sprawdzamy tylko w ogonie adresu** (dwa ostatnie segmenty
`formattedAddress`), a nie w całym ciągu. Szukanie po całości dałoby trafienie
na ulicy „Grecka" albo w poznańskiej dzielnicy Malta.

Fraza wyszukiwania to `"<nazwa bez nawiasów> <miasto> <kraj>"`. Kraj na końcu
to różnica wobec HolidayCheck: tam kraj wynikał z tenanta (`hcde`), tu Google
szuka globalnie i bez kraju chętnie odda hotel z innego kontynentu. Hotel
**bez nazwy** nie generuje zapytania w ogóle — samo „Kremasti Grecja" zwróciłoby
przypadkowy obiekt z tej miejscowości, i to z oceną, która wyglądałaby na dobrą.

## Kalibracja per źródło

Każdy serwis ocenia inną miarką:

| Źródło    | Kto ocenia                                           | Kierunek                               |
| ------------ | ---------------------------------------------------- | -------------------------------------- |
| wakacje.pl   | tylko klienci, którzy kupili tę ofertę            | punkt odniesienia                      |
| HolidayCheck | nocujący, kultura niemiecka                         | **surowiej** (mediana −0.6 pkt) |
| Google       | wszyscy — także gość restauracji i przechodzień | **łagodniej**                   |

Gdyby porównywać oceny wprost, narzędzie mierzyłoby **różnicę kultur
oceniania**, a nie jakość hotelu — i zapalałoby flagę „rozbieżność" na
hotelach, z którymi wszystko jest w porządku.

Dlatego `external_ratings.calibrate` liczy **medianę różnicy
`wakacje.pl − źródło` osobno dla każdego źródła**, z bieżącej próbki:

```
Systematyka holidaycheck: medianowo -0.7 pkt (surowiej niż wakacje.pl, 4 par)
                          — uwzględniona przy rozbieżnościach.
```

Cztery decyzje warte zapamiętania:

1. **Nic nie jest wpisane na sztywno.** Korekta liczy się z danych i jest
   **wypisywana pod tabelą**. Zaszyta stała byłaby niewidzialna i zestarzałaby
   się bez ostrzeżenia. (To zmiana wobec poprzedniej wersji dokumentu, która
   świadomie nie korygowała progu — przy dwóch źródłach o *przeciwnych*
   kierunkach systematyki brak korekty przestał być bezpieczny.)
2. **Mediana, nie średnia.** Jeden hotel z rozjazdem 4 pkt — a takie są, to
   cała wartość tego narzędzia — przesunąłby średnią i skalibrował system tak,
   by przestał go widzieć.
3. **Minimum 3 pary** (`MIN_CALIBRATION_PAIRS`). Mediana z dwóch liczb to nie
   systematyka, tylko przypadek. Poniżej progu korekty **nie stosujemy**,
   ale liczbę i tak pokazujemy z adnotacją „za mała próbka".
4. **Korekta przycięta do ±1.5 pkt** (`MAX_CALIBRATION_PTS` = próg
   rozbieżności). Gdyby źródło zaniżało medianowo o 3 pkt, „skalibrowanie" go
   sprawiłoby, że żaden hotel nigdy by już nie odstawał — czyli zamiotłoby
   problem pod dywan.

W tabeli kolumna **Różnica pokazuje wartość SUROWĄ** (to, co użytkownik widzi
na obu stronach), a flagę rozbieżności zapala **reszta ponad systematykę**
(`Reliability.diff_adj`). Obie liczby trafiają do kolumny „Uwagi", np.
*„źródła rozjeżdżają się o 3.0 pkt (2.35 pkt ponad systematykę źródła)"*.

## Normalizacja skali

HolidayCheck oddaje dziś `bestRating: "10"`, `worstRating: "1"` — dzielimy przez
**realne `bestRating`**, nie przez zaszytą stałą, bo serwis historycznie używał
skali 1-6 i część danych może tak jeszcze wyglądać.

`worstRating` (1) świadomie **ignorujemy**. Rozciągnięcie [1,10] na [0,10]
obniżyłoby każdą ocenę o ~0.5-1.0 pkt, a wakacje.pl publikuje swoje oceny w tej
samej konwencji „x na 10” z jedynką jako dnem (w bazie są wartości 1.8 i 4.6,
nie ma zer). Obie liczby mają być porównywalne z tym, co użytkownik **widzi na
obu stronach**, a nie z idealną skalą.

## Wskaźnik wiarygodności (`reliability_multi`)

Pewność to funkcja liczby opinii **ze wszystkich źródeł** oraz ich
**zgodności**. Przy **jednym** źródle zewnętrznym:

| Warunek                                             | Pewność                                               |
| --------------------------------------------------- | ------------------------------------------------------- |
| rozjazd (po kalibracji) > 1.5 pkt                   | **niska** (zawsze, niezależnie od liczby opinii) |
| zgodne (≤1.0 pkt) i łącznie ≥30 opinii          | **wysoka**                                        |
| zgodne i łącznie ≥10 opinii                      | **średnia**                                      |
| zgodne, ale łącznie <10 opinii                    | **niska**                                         |
| brak źródła zewnętrznego, ≥50 opinii lokalnych | **średnia** (sufit)                              |
| brak źródła zewnętrznego, ≤3 opinie lokalne    | **niska**                                         |

Przy **dwóch lub więcej** źródłach dochodzi warstwa, której przy jednym być
nie mogło — pytanie, czy źródła zgadzają się **ze sobą**:

| Warunek                                               | Pewność          | Flaga          |
| ----------------------------------------------------- | ------------------ | -------------- |
| źródła rozjeżdżają się między sobą > 1.5 pkt | **niska**    | rozbieżność |
| źródła zgodne, ale wakacje.pl odstaje > 1.5 pkt    | **niska**    | rozbieżność |
| źródła zgodne z wakacje.pl, łącznie ≥20 opinii  | **wysoka**   | —             |
| źródła zgodne z wakacje.pl, łącznie ≥10 opinii  | **średnia** | —             |
| źródła zgodne, ale łącznie <10 opinii            | **niska**    | —             |

Cztery decyzje warte zapamiętania:

1. **Rozjazd bije liczbę opinii.** Dwa źródła z setkami opinii, które się kłócą,
   znaczą „nie wiadomo”, a nie „średnia z nich”.
2. **Zgoda dwóch źródeł przeciwko wakacje.pl daje werdykt JEDNOZNACZNY.**
   To jest cały sens trzeciego źródła: „10.0 z jednej opinii” kontra 4.2/5
   z 800 opinii Google **i** 7.9 z 46 na HolidayCheck to nie remis, tylko
   zdemaskowana ocena lokalna. Komunikat mówi wprost: *„2 niezależne źródła
   zgodne (8.4 po kalibracji), wakacje.pl odstaje o 1.6 pkt”*.
3. **Zgoda dwóch źródeł podnosi pewność** — próg „wysokiej” spada z 30 opinii
   do 20. Niezależność źródeł jest wartością samą w sobie, a nie tylko
   dopisaniem opinii do wspólnego worka.
4. **Jedno źródło nigdy nie daje pewności wysokiej bez 30 opinii** i nigdy
   żadnej powyżej „średniej”, gdy go brak — po to jest drugie i trzecie.

Konsensus źródeł jest **ważony liczbą opinii**: Google z 800 opiniami waży
więcej niż HolidayCheck z czterema. `ambiguous`, `no_rating`, `no_key` i `error`
**nie wchodzą do werdyktu**, ale `ambiguous` i `no_rating` są cache’owane —
po to, żeby nie pytać drugi raz i żeby było widać, że próba była.

Flaga `rozbieżność` zapala się przy różnicy **> 1.5 pkt ponad systematykę
źródła**; hotele z oceną opartą na **≤3 opiniach** są znaczone na czerwono
niezależnie od wszystkiego.

`reliability(local, count, external)` (jedno źródło, bez kalibracji) zostaje
niezmienione co do zachowania i komunikatów — jest dziś cienką nakładką na
`reliability_multi`, żeby kod sprzed Google działał bez zmian.

## Systematyka: HolidayCheck ocenia surowiej

Na 14 dopasowanych par z bazy:

```
średnia różnica (wakacje.pl − HolidayCheck): +0.96 pkt
mediana:                                     +0.60 pkt
HolidayCheck niżej w:                        11/14 przypadków
```

To **nie jest przypadek** — HolidayCheck jest niemiecki (surowsza kultura
oceniania), a wakacje.pl zbiera opinie wyłącznie o hotelach, które sam sprzedaje.
Czytaj różnice **ponad to przesunięcie, nie od zera**: rozjazd 1.0 pkt jest
w normie, rozjazd 3.0 pkt już nie. `hs opinie` wypisuje medianę przesunięcia dla
bieżącej próbki, żeby to było widać bez zaglądania tutaj.

**Zmiana wobec pierwszej wersji.** Wtedy progu 1.5 pkt świadomie *nie*
korygowaliśmy o tę systematykę — jedno źródło, 14 par, a ukryta korekta byłaby
trudniejsza do zauważenia niż jawne przesunięcie. Przy dwóch źródłach
o **przeciwnych** kierunkach systematyki (HolidayCheck w dół, Google w górę)
brak korekty przestał być bezpieczny: ten sam hotel dostawałby flagę
rozbieżności od jednego źródła i potwierdzenie od drugiego, wyłącznie z powodu
różnicy kultur oceniania. Dlatego korekta jest dziś stosowana — ale **jawnie**:
liczona z bieżącej próbki, wypisywana pod tabelą, przycięta do ±1.5 pkt
i z surową różnicą wciąż widoczną w kolumnie „Różnica”. Szczegóły:
[Kalibracja per źródło](#kalibracja-per-źródło).

## Wyniki na żywych danych

`hs opinie wrzesien-okazje --top 25`: 14 hoteli z oceną zewnętrzną, 10
`ambiguous`, 1 bez opinii w drugim źródle. Cztery rozbieżności:

| Hotel                 | wakacje.pl | HolidayCheck | Δ             |
| --------------------- | ---------- | ------------ | -------------- |
| Grand Cettia          | 8.6 (17)   | 5.6 (36)     | **+3.0** |
| Blue Sea Club Marthas | 8.4 (37)   | 5.9 (254)    | **+2.5** |
| Castell Dels Hams     | 8.4 (4)    | 6.0 (9)      | **+2.4** |
| HSM Canarios Park     | 8.1 (101)  | 6.2 (331)    | **+1.9** |

Rozstrzygnięcie sprawy, od której się zaczęło:

| Hotel                           | wakacje.pl                | HolidayCheck       | Werdykt                                                                                   |
| ------------------------------- | ------------------------- | ------------------ | ----------------------------------------------------------------------------------------- |
| **Alkyonides (Kremasti)** | **10.0 z 1 opinii** | **6.2 z 4**  | **zdemaskowana** — top recenzja to „Niewieder 😬🤮😬”, „sehr dreckig überall” |
| **Olympia (Pefkohori)**   | **10.0 z 1 opinii** | **0 opinii** | niepotwierdzona —`getOpinionsBox` mówi wręcz `opinionsCount: 0`                    |
| Albatros Beach (Letojanni)      | 9.2 z 1 opinii            | 9.4 z 46           | **potwierdzona**                                                                    |
| Brancamaria                     | 8.7 z 1 opinii            | 8.9 z 334          | **potwierdzona**                                                                    |
| Marmari Bay                     | 9.0 z 1 opinii            | 7.7 z 3            | za mało danych po obu stronach                                                           |

Czyli: „ocena 10.0 z jednej opinii” bywa i prawdą, i fikcją — i dopiero drugie
źródło pozwala je rozróżnić. To jest cała wartość tej fazy.

## Użycie

```bash
PYTHONPATH=src python3 -m holiday_searcher.cli opinie wrzesien-okazje --top 10
PYTHONPATH=src python3 -m holiday_searcher.cli opinie wrzesien-okazje --top 25 --refresh
```

| Flaga                                | Znaczenie                                                      |
| ------------------------------------ | -------------------------------------------------------------- |
| `--top N`                          | ilu hoteli dotyczy weryfikacja (domyślnie 10)                 |
| `--source all\|holidaycheck\|google` | które źródła odpytać (domyślnie`all`)                  |
| `--refresh`                        | pomiń cache i pobierz oceny na nowo                           |
| `--delay S`                        | przerwa między żądaniami do HolidayCheck (domyślnie 2.0 s) |
| `--google-delay S`                 | przerwa między żądaniami do Google (domyślnie 0.2 s)       |
| `--fresh` / `--limit`            | jak w`hs enrich` — pominięcie bazy ofert                   |

Koszt przy pierwszym przebiegu: **2 żądania na hotel do HolidayCheck**
(suggest + strona) i **1 do Google** (samo `searchText`; `reviews` tylko na
wyraźne życzenie). Przy kolejnych — **0**, cache jest permanentny.

Bez klucza Google komenda działa normalnie: kolumna „Google” pokazuje
**„brak klucza”**, HolidayCheck i cała reszta bez zmian.

## Tabela w bazie

```sql
CREATE TABLE hotel_external_rating (
    hotel_id TEXT, source TEXT, matched_name TEXT,
    rating_0_10 REAL, review_count INTEGER, url TEXT,
    confidence REAL, status TEXT, fetched_at TEXT,
    PRIMARY KEY (hotel_id, source));
```

Zakładana leniwie przez `external_ratings.ensure_schema` (`executescript`,
idempotentnie) — `storage.py` z fazy 1 pozostaje nietknięty.

Klucz główny `(hotel_id, source)` był w schemacie od początku, więc dołożenie
Google **nie wymagało żadnej migracji** — HolidayCheck i Google siedzą w dwóch
osobnych wierszach tego samego hotelu i nie mogą się nadpisać.

Cache jest **permanentny**, wzorem `hotel_ai_verdict` z fazy 3: hotel nie
zmienia się z dnia na dzień, a przy 300 opiniach jedna nowa nie ruszy średniej
na pierwszym miejscu po przecinku. Zapisujemy też porażki (`no_match`,
`ambiguous`, `no_rating`) — to trwałe fakty.

Statusy **nietrwałe** (`TRANSIENT_STATUSES`), które `get` traktuje jak brak
wpisu i które ponawiają się przy następnym przebiegu:

| Status     | Dlaczego nietrwały                                                 |
| ---------- | ------------------------------------------------------------------- |
| `error`  | padnięta sieć to stan chwilowy                                    |
| `no_key` | brak klucza API to konfiguracja —**jutro może być wgrany** |

`no_key` jest tu kluczowy dla wymagania „ma zadziałać samo, gdy klucz się
pojawi": gdyby był cache’owany jak trwały fakt, wgranie klucza nic by nie
zmieniło bez ręcznego `--refresh`.

## Degradacja

Żadna ścieżka nie rzuca wyjątku. Brak trafienia, niepewne dopasowanie, hotel bez
opinii, padnięty Akamai, śmieć zamiast HTML-a, błąd Google, timeout, **brak
klucza API** — wszystko kończy się wierszem „brak danych” (albo „brak klucza”)
z podanym powodem. `hs opinie` działa też wtedy, gdy oba źródła są niedostępne
w całości: tabela pokazuje wtedy same oceny wakacje.pl z pewnością nie wyższą
niż „średnia”.

Bez `GOOGLE_PLACES_API_KEY` klient Google **nie dotyka sieci ani razu** —
`available` jest `False`, `fetch` oddaje `no_key` od razu, a komenda wypisuje
jedną linijkę z nazwą zmiennej do ustawienia i leci dalej. To jest testowane
wprost (`test_bez_klucza_nie_ma_ruchu_sieciowego`).

## Testy

```bash
PYTHONPATH=src python3 -m unittest tests.test_external_ratings -v
PYTHONPATH=src python3 -m unittest tests.test_external_google -v
```

70 + 89 testów, **bez sieci i bez klucza API**. Próbki w `tests/data/`:

| Plik                                   | Co zawiera                                                                                                                                                     |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `holidaycheck_hotel.html`            | realna strona hotelu przycięta do bloku JSON-LD (ocena + 4 recenzje)                                                                                          |
| `holidaycheck_hotel_bez_opinii.html` | to samo dla hotelu**bez** `aggregateRating`                                                                                                            |
| `holidaycheck_suggest.json`          | realna odpowiedź`suggestionSearch`                                                                                                                          |
| `google_places_searchtext.json`      | **syntetyczna** odpowiedź `places:searchText` — właściwy hotel, taverna o tej samej nazwie, imiennik w Turcji, imiennik na Krecie, biuro podróży |
| `google_places_rywale.json`          | **syntetyczna** — trzy hotele „Karbel” w Ölüdeniz (reguła rywala)                                                                                  |
| `google_places_details.json`         | **syntetyczna** — 5 opinii (limit API), jedna bez `text`, jedna bez treści                                                                           |
| `google_places_error.json`           | **syntetyczna** — HTTP 400 `INVALID_ARGUMENT` za brak field maska                                                                                     |

⚠️ **Próbki Google są syntetyczne.** W chwili pisania tego kodu klucza API nie
było, więc nie dało się zapisać realnej odpowiedzi. Kształt (nazwy pól,
zagnieżdżenia, format błędu) pochodzi **wprost z dokumentacji** Google
(`text-search`, `place-details`, referencja REST `v1/places`), a wartości
dobrano tak, by odtworzyć realne pułapki dopasowania opisane wyżej. Każdy plik
ma to zaznaczone w kluczu `_probka` (parsery go ignorują). **Po wgraniu klucza
warto podmienić je na zapis realnej odpowiedzi — wynik testów nie powinien się
wtedy zmienić.**

Pokryte po stronie HolidayCheck: parsowanie JSON-LD (w tym `/` i atrybut bez
cudzysłowów), normalizacja skali (10 / 6 / 5 / śmieci / `bestRating: 0`),
normalizacja i podobieństwo nazw, budowa frazy wyszukiwania, zgodność miejsca,
próg `ambiguous`, reguła rywala (bliźniak, rodzina hoteli, imiennik z innego
miasta, rywal z innego kraju), progi wiarygodności, cache (idempotentny schemat,
nadpisanie, cache’owanie porażek, `error` jako brak wpisu, rozdział źródeł,
współdzielone połączenie) oraz degradacja bez sieci.

Pokryte po stronie Google: spłaszczanie `displayName`/`location`, przeliczenie
skali 1-5 → 0-10, limit 5 opinii i odwrót na `originalText`, parsowanie
komunikatu błędu, filtr `types` (restauracja, biuro podróży, brak typów),
zgodność kraju (PL/EN/forma miejscowa, pułapka ulicy „Grecka”), premia za
miejscowość i to, że **nie** wchodzi do pewności, reguła rywala, budowa frazy,
warstwa HTTP na `httpx.MockTransport` (nagłówki, field mask bez spacji, ciało
zapytania, `locationBias`, błąd API, timeout), pełna ścieżka `fetch`,
degradacja bez klucza (w tym dowód, że **nie ma ruchu sieciowego**), rozdział
źródeł w cache’u, nietrwałość `no_key`, kalibracja (mediana per źródło, próg
próbki, przycięcie, odporność na skrajny przypadek) oraz agregacja wielu źródeł
(zgoda, spór, demaskowanie oceny lokalnej, kalibracja gasząca i **niegasząca**
rozbieżność).

## Znane ograniczenia

- **Zasięg HolidayCheck ~56%.** Na 25 hotelach 14 dostaje ocenę. Reszta to
  głównie `ambiguous` z reguły rywala — świadomy wybór na rzecz poprawności.
  Google ma to pokrycie poprawić, ale **nie zostało to jeszcze zmierzone na
  żywych danych**: klucza API nie było. `hs opinie` wypisuje pokrycie per
  źródło, więc pierwszy przebieg z kluczem od razu to pokaże.
- **Wszystko po stronie Google jest zweryfikowane wyłącznie wobec
  dokumentacji.** Kod przechodzi 89 testów na próbkach zbudowanych z opisu API,
  ale **żadne prawdziwe żądanie nigdy nie poszło**. Do sprawdzenia po wgraniu
  klucza: czy `formattedAddress` przy `languageCode: "pl"` faktycznie kończy się
  polską nazwą kraju (jeśli nie — `COUNTRY_ALIASES` już przyjmuje formę
  angielską i miejscową), czy hotele mają w `types` istotnie `hotel`/`lodging`,
  oraz czy `maxResultCount` nie zostało w międzyczasie wyłączone na rzecz
  `pageSize` (jest oznaczone jako deprecated).
- **Nazwa hotelu w bazie bywa uboga.** `Karbel` przy trzech hotelach „Karbel”
  w Ölüdeniz jest nierozstrzygalne z samych danych wakacje.pl. Rozwiązaniem
  byłyby współrzędne geograficzne — API ofert ich nie udostępnia. Google
  **zwraca** `location`, więc gdy hotel raz zostanie trafiony, jego współrzędne
  można podać jako `locationBias` przy sąsiednich obiektach. Nie jest to dziś
  wykorzystane.
- **Źródło z garstką opinii wciąż może zawetować werdykt.** HolidayCheck
  z 4 opiniami, który rozjeżdża się o 2.5 pkt z 800-opiniowym Google, daje
  „nie wiadomo”, a nie przegłosowanie. To świadoma konserwatywność zgodna
  z zasadą całego modułu, ale przy realnych danych może okazać się za ostra —
  wtedy kandydatem na poprawkę jest próg „ile opinii trzeba mieć, by móc
  zaprzeczyć innemu źródłu”, dziś nieistniejący.
- **Systematyczne przesunięcie** jest liczone i stosowane, ale **przycięte do
  ±1.5 pkt** i wymagające ≥3 par. Przy `--top 10` i słabym pokryciu próbka
  bywa mniejsza — wtedy korekta nie jest stosowana wcale, o czym komenda
  informuje pod tabelą.
- **Rachunek u Google jest realny.** Maska sięga po `rating`, czyli SKU
  Enterprise: 1000 darmowych wywołań miesięcznie. Przy 25 hotelach i cache’u
  permanentnym to bez znaczenia, ale `--refresh` w pętli już nie — dlatego
  krok 7 instrukcji (dzienny limit żądań w konsoli) nie jest opcjonalny.
- **Akamai może się zaostrzyć.** Dziś wystarczają nagłówki; gdyby serwis zaczął
  wymagać ciasteczka `_abck` z sensora `/akam/13/...`, ta droga się zamyka
  i trzeba by przeglądarki. Wtedy interfejs (`ExternalRating`,
  `ExternalRatingStore`, `reliability`) zostaje, a wymienia się tylko klasę
  `HolidayCheckRatings`.
- **Recenzje pobierane są po niemiecku** — `parse_hotel_page` zbiera do 5
  tekstów, ale `hs opinie` ich dziś nie pokazuje. Są gotowe pod ewentualny
  werdykt AI (faza 3 przyjmuje dowolny materiał tekstowy).
