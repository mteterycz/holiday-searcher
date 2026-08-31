"""Podkomenda `hs opinie` — konfrontacja oceny wakacje.pl ze źródłami zewnętrznymi.

Pytanie, na które ta komenda odpowiada, brzmi: **czy tej ocenie wolno wierzyć**.
Profil filtruje oferty progiem `rating_min: 8.0`, a spora część hoteli ma tę
ocenę z 1-3 opinii — czyli próg stoi na szumie. Tabela stawia obok siebie
ocenę z wakacje.pl, ocenę z HolidayCheck, ocenę z Google i liczby opinii po
wszystkich stronach, a potem mówi wprost, czy to się broni.

DWA ŹRÓDŁA ZEWNĘTRZNE, NIE JEDNO
--------------------------------
HolidayCheck jest niezależny i szczegółowy, ale wąski (14 trafień na 25 hoteli).
Google zna prawie każdy obiekt i ma setki opinii tam, gdzie wakacje.pl ma jedną.
Razem dają coś, czego żadne z osobna nie daje: gdy oba zgadzają się ze sobą
i nie zgadzają z wakacje.pl, werdykt jest jednoznaczny, a nie remisowy.

KALIBRACJA — DLACZEGO KOLUMNY NIE SĄ PORÓWNYWALNE WPROST
--------------------------------------------------------
Każdy serwis ocenia inną miarką: HolidayCheck jest niemiecki i ocenia surowiej,
Google pyta szeroką publiczność (także gości restauracji, nie tylko nocujących)
i ocenia łagodniej. Dlatego przed liczeniem rozbieżności odejmujemy medianowe
przesunięcie KAŻDEGO źródła, policzone z bieżącej próbki — inaczej narzędzie
mierzyłoby różnice kultur oceniania zamiast jakości hoteli. Przesunięcia są
wypisywane pod tabelą, żeby korekta nie była niewidzialna.

Jak w `cli_ext/ai.py`: brak trafienia, padnięte źródło i brak klucza API dają
wiersz „brak danych", nigdy wyjątek.
"""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..external_google import API_KEY_NAME, SOURCE as SRC_GOOGLE, GooglePlacesRatings
from ..external_ratings import (
    DIVERGENCE_PTS, MIN_CALIBRATION_PAIRS, SOURCE as SRC_HC, ST_AMBIGUOUS,
    ST_ERROR, ST_NO_KEY, ST_NO_MATCH, ST_NO_RATING, THIN_EVIDENCE,
    ExternalRatingStore, HolidayCheckRatings, calibrate, get_or_fetch,
    offsets_map, reliability_multi,
)
from ..paths import DB_PATH

console = Console()

# Kolory pewności — ten sam kierunek co w reszcie CLI (czerwone = uwaga).
_KOLOR = {"wysoka": "green", "średnia": "yellow", "niska": "red"}

_OPIS_STATUSU = {
    ST_NO_MATCH: "brak danych (nie znaleziono)",
    ST_AMBIGUOUS: "brak danych (niepewne dopasowanie)",
    ST_NO_RATING: "brak danych (0 opinii)",
    ST_ERROR: "brak danych (źródło niedostępne)",
    ST_NO_KEY: "brak klucza",
}

# Etykiety kolumn. Kolejność ma znaczenie: HolidayCheck jest starszym źródłem
# i stoi tam, gdzie stał, żeby nawyk czytania tabeli się nie zmienił.
_ETYKIETY = {SRC_HC: "HolidayCheck", SRC_GOOGLE: "Google"}
_WYBOR = {"holidaycheck": [SRC_HC], "google": [SRC_GOOGLE], "all": [SRC_HC, SRC_GOOGLE]}


def cmd_opinie(args) -> None:
    from ..cli import load_profile
    from ..storage import Storage
    from .ai import _load_candidates, _top_hotels

    profile = load_profile(args.profile)
    store = Storage(DB_PATH)
    scored = _load_candidates(args, profile, store.db)
    if not scored:
        console.print("[yellow]Brak ofert dla tego profilu — najpierw `hs search`.[/]")
        return

    hotels = _top_hotels(scored, args.top)
    cache = ExternalRatingStore(store.db)

    wybrane = _WYBOR[args.source]
    klienci = {}
    if SRC_HC in wybrane:
        klienci[SRC_HC] = HolidayCheckRatings(delay=args.delay)
    if SRC_GOOGLE in wybrane:
        klienci[SRC_GOOGLE] = GooglePlacesRatings(delay=args.google_delay)

    google = klienci.get(SRC_GOOGLE)
    opis_zrodel = " + ".join(
        _ETYKIETY[s] + ("" if s != SRC_GOOGLE or google.available else " [dim](brak klucza)[/]")
        for s in wybrane
    )
    console.print(
        f"[bold]{profile.name}[/] — top {len(hotels)} hoteli wg scoringu, "
        f"źródła zewnętrzne: [bold]{opis_zrodel}[/]"
    )
    if google is not None and not google.available:
        # Brak klucza to konfiguracja, nie awaria — mówimy, co zrobić, i lecimy dalej.
        console.print(
            f"[dim]Google pominięty: ustaw [bold]{API_KEY_NAME}[/bold] w zmiennych "
            f"środowiskowych albo w `config/.env` (szczegóły: "
            f"docs/opinie-zewnetrzne.md). Reszta działa normalnie.[/]"
        )
    if not args.refresh:
        console.print("[dim]Cache permanentny — `--refresh` wymusza ponowne pobranie.[/]")

    # Krok 1: zebrać wszystko. Kalibracji nie da się policzyć wcześniej,
    # bo liczy się ją z tej właśnie próbki, a nie ze stałej w kodzie.
    zebrane = []
    for s in hotels:
        o = s.offer
        oceny = {
            src: get_or_fetch(cache, klient, o.hotel_id, o.hotel_name,
                              country=o.country, city=o.city, region=o.region,
                              refresh=args.refresh)
            for src, klient in klienci.items()
        }
        zebrane.append((s, oceny))

    # Krok 2: systematyka per źródło z bieżącej próbki.
    kalibracja = calibrate(
        (s.offer.rating, ext) for s, oceny in zebrane for ext in oceny.values()
    )
    offsets = offsets_map(kalibracja)

    # Krok 3: werdykty już z uwzględnieniem systematyki.
    wiersze = [
        (s, oceny, reliability_multi(s.offer.rating, s.offer.rating_count,
                                     list(oceny.values()), offsets))
        for s, oceny in zebrane
    ]

    console.print(_tabela(wiersze, profile.name, wybrane))
    _podsumowanie(wiersze, wybrane, kalibracja, offsets)


def _komorka(ext) -> str:
    """Jedna komórka źródła: ocena 0-10 i liczba opinii, albo powód braku."""
    if ext is None:
        return "[dim]–[/]"
    if ext.usable:
        return f"{ext.rating:.1f} ({ext.review_count or 0})"
    opis = _OPIS_STATUSU.get(ext.status, "brak danych")
    kolor = "yellow" if ext.status == ST_NO_KEY else "dim"
    return f"[{kolor}]{opis}[/]"


def _tabela(wiersze, nazwa_profilu: str, zrodla: list[str]) -> Table:
    t = Table(title=f"Weryfikacja ocen hoteli — {nazwa_profilu}", header_style="bold")
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Hotel", max_width=24, overflow="ellipsis")
    t.add_column("wakacje.pl", justify="right", width=10)
    for src in zrodla:
        t.add_column(_ETYKIETY[src], justify="right", width=12 if src == SRC_HC else 11)
    t.add_column("Różnica", justify="right", width=7)
    t.add_column("Pewność", width=8)
    t.add_column("Uwagi", max_width=34, overflow="fold")

    for i, (s, oceny, rel) in enumerate(wiersze, 1):
        o = s.offer
        lokalna = "–" if o.rating is None else f"{o.rating:.1f}"
        n_lok = o.rating_count or 0
        # Cienki materiał dowodowy to główny powód istnienia tej komendy —
        # musi kłuć w oczy, a nie chować się w kolumnie „uwagi".
        lok_txt = f"{lokalna} ({n_lok})"
        if n_lok <= THIN_EVIDENCE:
            lok_txt = f"[red]{lokalna} ({n_lok})[/]"

        roznica = "–" if rel.diff is None else f"{rel.diff:.1f}"
        if rel.divergent:
            roznica = f"[red bold]{roznica} ![/]"

        uwagi = []
        if rel.divergent:
            uwagi.append(f"[red bold]ROZBIEŻNOŚĆ[/] (>{DIVERGENCE_PTS} pkt)")
        if rel.agreement and not rel.divergent:
            uwagi.append("[green]dwa źródła zgodne[/]")
        if n_lok <= THIN_EVIDENCE and (o.rating or 0) >= 8.0:
            uwagi.append(f"[red]ocena {lokalna} stoi na {n_lok} opinii[/]")
        uwagi.append(f"[dim]{rel.reason}[/]")

        kolor = _KOLOR.get(rel.level, "")
        t.add_row(str(i), o.hotel_name, lok_txt,
                  *[_komorka(oceny.get(src)) for src in zrodla],
                  roznica, f"[{kolor}]{rel.level}[/]", " · ".join(uwagi))
    return t


def _podsumowanie(wiersze, zrodla, kalibracja, offsets) -> None:
    rozbiezne = [w for w in wiersze if w[2].divergent]
    cienkie = [w for w in wiersze if (w[0].offer.rating_count or 0) <= THIN_EVIDENCE]
    potwierdzone = [w for w in wiersze if w[2].level == "wysoka"]
    bez_danych = [w for w in wiersze if not w[2].sources]

    console.print(
        f"\n[bold]{len(wiersze)}[/] hoteli · "
        f"[green]{len(potwierdzone)}[/] z oceną potwierdzoną · "
        f"[red]{len(rozbiezne)}[/] z rozbieżnością · "
        f"[red]{len(cienkie)}[/] z oceną z ≤{THIN_EVIDENCE} opinii · "
        f"[dim]{len(bez_danych)} bez żadnego źródła zewnętrznego[/]"
    )

    # Pokrycie per źródło — po to dokładaliśmy Google, więc musi być widać,
    # ile realnie dołożył ponad HolidayCheck.
    for src in zrodla:
        trafienia = sum(1 for _, oceny, _ in wiersze
                        if oceny.get(src) is not None and oceny[src].usable)
        console.print(f"[dim]Pokrycie {_ETYKIETY[src]}: {trafienia}/{len(wiersze)} hoteli.[/]")

    for s, oceny, r in rozbiezne[:5]:
        o = s.offer
        console.print(
            f"[red]· {o.hotel_name}:[/] wakacje.pl {o.rating} z {o.rating_count or 0} opinii "
            f"vs {r.reason}"
        )

    # Systematyka: bez tych liczb użytkownik czytałby całe przesunięcie jako
    # wadę hotelu, a część z niego to wada porównania.
    for src in zrodla:
        off = kalibracja.get(src)
        if off is None:
            continue
        if off.enough:
            stan = ("uwzględniona przy rozbieżnościach" if src in offsets
                    else "pomijalna, nie korygujemy")
        else:
            stan = f"za mała próbka (<{MIN_CALIBRATION_PAIRS} par) — NIE korygujemy"
        console.print(f"[dim]Systematyka {off.label} — {stan}.[/]")
    if offsets:
        console.print("[dim]Różnice czytaj ponad te przesunięcia, nie od zera.[/]")

    if bez_danych:
        console.print(
            "[dim]Bez źródła zewnętrznego nie podnosimy pewności powyżej średniej — "
            "jedno źródło to wciąż jedno źródło.[/]"
        )


def register(sub) -> None:
    p = sub.add_parser(
        "opinie",
        help="konfrontacja ocen wakacje.pl ze źródłami zewnętrznymi "
             "(HolidayCheck + Google Places)",
    )
    p.add_argument("profile")
    p.add_argument("--top", type=int, default=10, help="ilu hoteli dotyczy weryfikacja")
    p.add_argument("--limit", type=int, default=120, help="ile ofert pobrać, gdy baza pusta")
    p.add_argument("--fresh", action="store_true", help="pomiń bazę, pobierz świeże oferty")
    p.add_argument("--refresh", action="store_true",
                   help="pomiń cache ocen zewnętrznych i pobierz je na nowo")
    p.add_argument("--source", choices=sorted(_WYBOR), default="all",
                   help="które źródła zewnętrzne odpytać (domyślnie oba)")
    p.add_argument("--delay", type=float, default=2.0,
                   help="przerwa między żądaniami do HolidayCheck [s]")
    p.add_argument("--google-delay", type=float, default=0.2,
                   help="przerwa między żądaniami do Google Places [s]")
    p.set_defaults(func=cmd_opinie)
