# Faza 2 — monitoring cen i powiadomienia Telegram

## Jak to działa

`hs monitor <profil>` to jeden pełny cykl:

1. pobiera oferty przez `WakacjeProvider` (jak `hs search`, ale bez próbki
   referencyjnej i bez liczenia scoringu — monitoring nie ocenia ofert,
   tylko śledzi ich ceny),
2. zapisuje je przez `Storage.save()` — każdy przebieg dokłada nowe
   snapshoty cen do `price_snapshot` (tabela jest append-only, więc historia
   nigdy nie ginie),
3. woła `holiday_searcher.deals.scan_for_events()`, które wykrywa:
   - **PRICE_DROP** — najnowsza cena oferty spadła o co najmniej
     `--drop-pct` (domyślnie 5%) względem poprzedniego snapshotu. Gdy
     oferta ma już co najmniej 5 snapshotów w ostatnich 30 dniach, dokłada
     się drugi warunek: nowa cena musi być poniżej 20. percentyla tego
     okna. To reguła rozkładowa, nie "punkt do punktu" — Mac śpi w nocy i
     w weekendy, więc odstępy między pomiarami są nierówne, a percentyl z
     okna czasowego jest na to odporny. Przy mniejszej historii (poniżej 5
     snapshotów) liczy się sam procentowy spadek — to tryb "last-minute",
     gdzie ważniejsza jest szybkość niż pewność statystyczna.
   - **PRICE_FLOOR** — bieżąca cena jest ŚCIŚLE najniższa w całej historii
     tej oferty, a historia ma co najmniej 5 momentów pomiaru. Orzeczenie
     pochodzi z `hotel_index` (`at_historic_low`) — tego samego wskaźnika,
     który napędza `hs indeks`, żeby alert i tabela nigdy nie mówiły dwóch
     różnych rzeczy. Szczegóły: `docs/indeks-cen.md`.
   - **NEW_OFFER** — oferta, która pojawiła się w bazie w ostatnich 24h.
   - **OFFER_VANISHED** — oferta była w poprzednim przebiegu profilu, a w
     bieżącym jej nie ma.
   - **PRICE_RISE** — symetryczne do spadku, ale trafia wyłącznie do
     raportu `hs diff`, nigdy do powiadomień.
4. filtruje zdarzenia przez anti-spam (`notification_log`): ta sama para
   (oferta, typ zdarzenia) idzie do użytkownika najwyżej raz na
   `--cooldown-days` (domyślnie 3 dni),
5. wysyła to, co zostało, przez Telegram (albo tylko wypisuje na konsolę
   przy `--dry-run` — wtedy cooldown NIE jest zużywany, więc bezpiecznie
   testować monitoring wielokrotnie).

`hs diff <profil>` to osobne, "ludzkie" narzędzie: pokazuje różnice między
dwoma ostatnimi przebiegami (`run`) danego profilu — zmiany cen (obie
strony, spadki i wzrosty), nowe oferty i te, które zniknęły. To nie jest
kanał powiadomień, tylko szybki przegląd "co się zmieniło od ostatniego
razu", więc pokazuje KAŻDĄ zmianę ceny, nie tylko te powyżej progu.
W sekcji „Zniknęły" najpierw idą oferty, które zniknęły PO obniżce.

## Które zdarzenia trafiają na Telegram

`NOTIFIABLE_EVENT_TYPES = ("PRICE_DROP", "NEW_OFFER", "PRICE_FLOOR",
"OFFER_VANISHED")`, ale OFFER_VANISHED przechodzi jeszcze jeden warunek.

| Typ            | Powiadomienie | Uzasadnienie                                     |
| -------------- | ------------- | ------------------------------------------------ |
| PRICE_FLOOR    | tak, osobno   | najmocniejszy sygnał, jaki mamy                  |
| PRICE_DROP     | tak, osobno   | jak dotąd                                        |
| OFFER_VANISHED | **warunkowo** | tylko gdy zniknięcie poprzedziła obniżka ≥ 5%    |
| NEW_OFFER      | tak, digest   | jak dotąd, jedna zbiorcza wiadomość              |
| PRICE_RISE     | nigdy         | materiał wyłącznie do `hs diff`                  |

### Dlaczego OFFER_VANISHED jest informacją, a nie alarmem

Zwykłe zniknięcie oferty z wyników **nie idzie na Telegram**. Trzy powody:

1. Przy godzinnym cyklu i twardym `--limit` oferty wpadają i wypadają z
   pobieranego zakresu bez przerwy — ranking dostawcy się przetasowuje,
   dostępność miga. To byłby szum, nie sygnał.
2. Ping o zniknięciu jest z natury niewykonalny: skoro oferty nie ma, nie ma
   czego kliknąć.
3. Zniknięcia widać i tak — w `hs diff` oraz w podsumowaniu `hs monitor`
   (sekcja „zniknęło z wyników bez obniżki", wypisywana na dim, bez wysyłki).

Powiadamiamy dopiero o zniknięciu **tuż po obniżce** (`is_sellout_signal`,
próg `VANISH_AFTER_DROP_PCT = 5%`). To niesie treść: potwierdza, że tamta
obniżka była prawdziwa, a ten hotel wyprzedaje tanie miejsca szybko — czyli
następnym razem trzeba reagować od razu. Takie zdarzenia idą jedną zbiorczą
wiadomością (`notify.format_vanished_digest`), bo zniknięcia lubią
przychodzić grupami, gdy touroperator zamyka pulę na jeden termin.

### Zabezpieczenie przed lawiną fałszywych zniknięć

`VANISH_MAX_SHARE = 0.5`: gdy z przebiegu na przebieg znika WIĘCEJ niż
połowa ofert, `detect_vanished_offers` nie zgłasza **niczego**. To prawie na
pewno nie wyprzedaż, tylko obcięty pobór — inny `--limit`, timeout dostawcy,
zmiana filtrów w profilu. Jedna cicha przerwa jest dużo tańsza niż
kilkadziesiąt fałszywych alarmów.

Przykład z życia: po przebiegu z `--limit 150` (60 ofert) uruchomienie
`hs monitor wrzesien-okazje --limit 40` daje 67% „zniknięć". Zabezpieczenie
wycisza wtedy 40 fałszywych zdarzeń. Wniosek praktyczny: **trzymaj stały
`--limit` w plist launchd**, inaczej detekcja zniknięć będzie się regularnie
wyłączać.

Dodatkowe warunki: pusty przebieg (0 ofert) nie zgłasza zniknięć w ogóle
(awaria pobierania, nie wyprzedaż kraju), a oferta z `last_seen` nowszym niż
start bieżącego przebiegu jest pomijana — ktoś ją widział (np. inny profil
albo `hs search`), więc nie zniknęła.

### PRICE_FLOOR zastępuje PRICE_DROP

Nowe minimum historii zawsze jest też spadkiem, więc bez tej reguły
dostawałbyś dwa pingi o jednym zdarzeniu. Gdy `scan_for_events` wykryje
PRICE_FLOOR dla oferty, nie zgłasza już dla niej PRICE_DROP. Analogicznie
oferta, która zniknęła, nie jest w tym samym skanie zgłaszana jako okazja —
„historyczne minimum, bierz" o czymś, czego nie da się kupić, byłoby
najgorszym możliwym powiadomieniem.

Warunek „ŚCIŚLE najniższa" (a nie „nie wyższa niż minimum") jest celowy:
cena stojąca płasko na minimum przez dziesięć przebiegów nie jest nowiną i
zapalałaby alert w kółko.

## Konfiguracja Telegrama

Bez konfiguracji `hs monitor` działa dalej — po prostu wypisuje wiadomości
na konsolę z adnotacją "Telegram nieskonfigurowany". Żeby dostawać
prawdziwe powiadomienia:

1. **Załóż bota przez @BotFather**
   - Otwórz Telegram, wyszukaj `@BotFather`, wyślij `/newbot`.
   - Podaj nazwę i unikalny login bota (musi kończyć się na `bot`).
   - BotFather odpowie tokenem w formacie `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
     — to jest `TELEGRAM_BOT_TOKEN`.

2. **Znajdź swój chat_id**
   - Napisz do swojego nowego bota dowolną wiadomość (np. "cześć") —
     Telegram wymaga, żeby użytkownik zainicjował rozmowę jako pierwszy.
   - Wejdź w przeglądarce na:
     `https://api.telegram.org/bot<TWÓJ_TOKEN>/getUpdates`
   - W odpowiedzi JSON znajdź `"chat":{"id": ...}` — ta liczba to
     `TELEGRAM_CHAT_ID` (dla rozmowy prywatnej zwykle dodatnia liczba, dla
     grupy — ujemna).

3. **Zapisz sekrety**
   - Skopiuj `config/.env.example` do `config/.env` (jeśli jeszcze nie
     istnieje) i uzupełnij:
     ```
     TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
     TELEGRAM_CHAT_ID=987654321
     ```
   - `config/.env` nie jest nigdzie wysyłane (patrz `.env.example`) —
     `config.get_secret()` czyta najpierw zmienne środowiskowe, potem ten
     plik.

4. **Test**
   ```bash
   PYTHONPATH=src python3 -m holiday_searcher.cli monitor turcja-wrzesien --limit 30
   ```
   Jeśli sekrety są poprawne, powiadomienia (jeśli jakieś wykryto) trafią
   na Telegram zamiast na konsolę.

## Instalacja harmonogramu (launchd)

Harmonogram uruchamia `hs monitor` trzy razy dziennie: **08:00, 14:30,
20:30**. Jeśli komputer śpi o tej porze, launchd odpali zadanie zaraz po
wybudzeniu (stąd percentylowa, a nie "punkt do punktu", reguła detekcji
obniżek — patrz wyżej).

1. Sprawdź, że plist wskazuje właściwą ścieżkę do `python3`:
   ```bash
   which python3
   ```
   i porównaj z `ProgramArguments` w
   `launchd/com.holiday-searcher.monitor.plist` — jeśli się różnią,
   podmień ścieżkę w pliku przed instalacją.

2. Zainstaluj:
   ```bash
   scripts/install-launchd.sh
   ```
   Skrypt kopiuje plist do `~/Library/LaunchAgents/`, wyładowuje starą
   wersję (jeśli była) i ładuje nową przez `launchctl bootstrap`.

3. Sprawdź status i logi:
   ```bash
   launchctl list | grep holiday-searcher
   tail -f data/monitor.log
   ```

4. Wymuś natychmiastowe uruchomienie (bez czekania na najbliższą godzinę):
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.holiday-searcher.monitor
   ```

5. Żeby wyłączyć harmonogram:
   ```bash
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.holiday-searcher.monitor.plist
   ```

Domyślnie plist monitoruje profil `turcja-wrzesien`. Żeby pilnować innego
profilu, zmień ostatni argument w `ProgramArguments` (albo skopiuj plik pod
inną nazwą `Label` i `ProgramArguments`, żeby monitorować kilka profili
równolegle — każdy potrzebuje własnego unikalnego `Label`).
