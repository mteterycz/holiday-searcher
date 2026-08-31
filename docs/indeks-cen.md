# Indeks cen hotelu — `hs indeks`

## Po co to jest

Scoring (`scoring.py`, kolumna „vs koszyk" w `hs search`) porównuje ofertę z
**rynkiem**: mediana podobnych hoteli w tym samym regionie, tej samej
kategorii gwiazdkowej i tej samej rodzinie wyżywienia. To dobre pytanie, ale
ma znane ograniczenie opisane w README: kandydaci pochodzą z taniego ogona,
a próbka referencyjna z ofert popularnych, więc mnożnik jest poprawny jako
porządek, a zawyżony jako wartość bezwzględna.

Indeks cen odpowiada na pytanie niezależne od tamtego obciążenia:
**czy ten konkretny hotel jest tani na tle SIEBIE SAMEGO?**

> „Ten hotel przez dwa tygodnie chodził po 2 400–3 100 zł. Teraz jest 2 350 —
> najniżej, odkąd go obserwujemy."

Takie zdanie jest mocniejszym sygnałem okazji niż mediana rynku, bo nie
zależy od tego, jak dobrany jest koszyk porównawczy. Nie zastępuje scoringu —
scoring mówi, czy hotel w ogóle warto rozważać, indeks mówi, czy to dobry
MOMENT.

## Uruchomienie

```bash
PYTHONPATH=src python3 -m holiday_searcher.cli indeks wrzesien-okazje --top 15
PYTHONPATH=src python3 -m holiday_searcher.cli indeks wrzesien-okazje --all
```

Kolumny:

| Kolumna       | Znaczenie                                                          |
| ------------- | ------------------------------------------------------------------ |
| `Teraz`       | bieżąca cena w **zł za osobę za noc**                              |
| `Pakiet`      | bieżąca cena za osobę za cały wyjazd (to, co widać w serwisie)      |
| `Min·Med·Max` | minimum, mediana i maksimum historii tego hotelu, też w zł/os/noc   |
| `Poz.`        | percentyl bieżącej ceny we własnej historii; niżej = lepiej         |
| `n`           | liczba snapshotów, po ukośniku liczba różnych **momentów** pomiaru  |
| `Okres`       | rozpiętość czasowa historii                                         |
| `Pewność`     | brak / niska / średnia / wysoka — patrz niżej                       |

Wyróżnienia: **zielona nazwa** = cena w dolnych 20% własnej historii,
**▼ przed nazwą** = historyczne minimum (orzekane dopiero od 5 momentów
pomiaru).

Sortowanie: najpierw hotele z wiarygodną historią, w kolejności percentyla
(czyli okazje na górze), potem reszta od najtańszej za noc. Hotel, o którym
nic nie wiemy, nie może wyprzedzić hotelu, o którym coś wiemy — to ta sama
zasada, co `UNRATED_CAP` w scoringu.

## Trzy decyzje, których nie należy odwracać

### 1. Agregujemy po HOTELU, nie po ofercie

Warianty tego samego hotelu (inny termin, inne wyżywienie, inny pokój) mają
osobne `offer.key`, ale to ta sama rzecz. Historia liczona per klucz oferty
byłaby pocięta na kawałki i przy godzinnym monitoringu prawie zawsze za
krótka, żeby cokolwiek znaczyć.

Kluczem grupującym jest para **(provider, hotel_id)**, a nie samo
`hotel_id`. Numeracja hoteli u wakacje.pl i r.pl jest niezależna, więc te
same cyfry u dwóch dostawców to dwa różne obiekty. Skutek uboczny: ten sam
fizyczny hotel widziany u obu dostawców daje dwa wiersze indeksu. Sklejanie
takich par to zadanie `dedup.py` i celowo nie jest tu powtarzane.

Gdy dostawca nie poda `hotel_id`, oferta jest swoim własnym „hotelem"
(fallback po kluczu, nigdy po nazwie — dwa różne hotele potrafią nazywać się
identycznie).

### 2. Liczymy w zł/os/noc, nie w cenie pakietu

W jednym hotelu współistnieją wyjazdy 5- i 11-nocne. Minimum z surowych cen
pakietu znaczyłoby wtedy tylko tyle, że ktoś kiedyś sprzedawał krótszy
pobyt — a nie że hotel staniał. `price_ppn` to jedyna wielkość, w której
wolno porównywać (ta sama zasada co w `models.Offer.price_ppn` i w
scoringu). Cenę pakietu pokazujemy obok, bo to w niej myśli człowiek.

### 3. Przy krótkiej historii moduł MILCZY, zamiast zmyślać

Każda pierwsza obserwacja jest jednocześnie minimum i maksimum. Gdyby indeks
raportował ją jako „historyczne minimum", kłamałby w każdym pierwszym
przebiegu. Dlatego:

- `HotelPriceIndex.percentile` to `None` przy jednym pomiarze (pozycji
  jednego punktu nie da się określić),
- `reliable` wymaga co najmniej `MIN_SAMPLES_FOR_CLAIM` (5) **momentów**
  pomiaru,
- `at_historic_low` i `in_bottom_zone` — jedyne pola, którymi wolno uzasadnić
  komunikat o okazji — są twardo zablokowane, dopóki `reliable` jest fałszem,
- percentyl policzony na 2–4 punktach pokazuje się w tabeli w nawiasie, jako
  ciekawostkę, a nie podstawę decyzji,
- `headline()` dobiera zdanie do tego, ile naprawdę wiemy.

Moduł nigdy nie rzuca wyjątkiem przy krótkiej historii — degraduje się.

**Snapshoty to nie to samo co momenty pomiaru.** Hotel sprzedawany w pięciu
wariantach daje pięć snapshotów w jednym przebiegu i ani jednego punktu
historii. Gdyby progiem była liczba snapshotów, taki hotel dostawałby
etykietę „historyczne minimum" zaraz po pierwszym pobraniu, opisując w
rzeczywistości tylko rozrzut cen między wariantami. Stąd `time_points`
(liczba różnych `ts`) i to na niej opiera się `reliable` oraz `confidence`.
W tabeli widać to jako `n = 5/2`: pięć snapshotów, dwa momenty.

### Skala pewności

| Etykieta  | Warunek                                            |
| --------- | -------------------------------------------------- |
| `brak`    | < 2 momenty pomiaru — nie ma historii              |
| `niska`   | 2–4 momenty — za mało, żeby orzekać o minimum      |
| `średnia` | ≥ 5 momentów                                       |
| `wysoka`  | ≥ 10 momentów **i** rozpiętość ≥ 3 dni             |

## Jak liczony jest percentyl

Bieżąca cena jest częścią własnej historii, więc pozycja liczona jest
midrankiem: `(ile punktów niżej + połowa remisów) / wszystkie punkty`.
Konsekwencje, które warto znać:

- płaska historia daje 0.5 (ani okazja, ani ostrzeżenie),
- jedyne minimum przy 10 punktach daje 0.05, a nie mylące zero,
- powtórzone minimum daje więcej niż 0.05 — bo to już nie jest nowina.

## Powiązanie z alertami

`deals.detect_price_floor()` używa dokładnie tego samego wskaźnika
(`hotel_index.offer_index(...).at_historic_low`), żeby alert PRICE_FLOOR i
tabela `hs indeks` nigdy nie mówiły dwóch różnych rzeczy. Szczegóły:
`docs/faza2-monitoring.md`.

Alert liczony jest na poziomie **pojedynczej oferty**, mimo że indeks
potrafi też pulę całego hotelu. Powód: hotel bywa sprzedawany w wariantach o
różnym poziomie cen (5 vs 11 nocy, BB vs AI), więc „rekord hotelu"
zapalałby się za każdym razem, gdy do wyników wpadnie tańszy wariant — a to
nie jest obniżka. Kontekst całego hotelu trafia do treści powiadomienia jako
tło.

## API modułu

```python
from holiday_searcher import hotel_index

hotel_index.offer_index(db, offer_key)          # historia jednej oferty
hotel_index.hotel_index(db, provider, hotel_id) # historia wszystkich wariantów
hotel_index.index_for_offer(db, offer_key)      # indeks hotelu, do którego należy oferta
hotel_index.build_all(db, profile="wrzesien-okazje")   # posortowana lista
```

Wszystkie zwracają `HotelPriceIndex` (albo `None`, gdy nie ma ani jednego
snapshotu) i nie modyfikują bazy.
