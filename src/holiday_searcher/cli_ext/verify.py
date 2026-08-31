"""CLI: hs weryfikuj — sprawdza, czy cena z listingu jest ceną, którą realnie
zapłacisz, czy tylko ceną „od".

Dla każdej oferty pobieramy z wakacje.pl listę rezerwowalnych wariantów pokoi
i porównujemy najtańszy z ceną, którą mamy w bazie. Szczegóły rekonesansu
i kształt endpointów: `docs/weryfikacja-ceny.md`.
"""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..cli import load_profile
from ..paths import DB_PATH
from ..storage import Storage
from ..verify import (
    SUSPICIOUS_PCT,
    TOLERANCE_PCT,
    PriceVerifier,
    offers_to_verify,
    save_verification,
)

console = Console()


def _money(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")


def _diff_cell(v) -> str:
    """Zielony do 2%, żółty 2–10%, czerwony powyżej. Kolor niesie werdykt,
    więc nie duplikujemy go osobną kolumną."""
    pct = v.diff_pct
    if pct is None:
        return "[dim]—[/]"
    pln = v.diff_pln
    text = f"{pln:+d} zł / {pct:+.1f}%"
    if abs(pct) <= TOLERANCE_PCT:
        return f"[green]{text}[/]"
    if abs(pct) <= SUSPICIOUS_PCT:
        return f"[yellow]{text}[/]"
    return f"[red]{text}[/]"


def cmd_verify(args):
    profile = load_profile(args.profile)   # czytelny błąd przy złej nazwie
    store = Storage(DB_PATH)

    offers = offers_to_verify(store.db, profile.name, top=args.top)
    if not offers:
        console.print(
            f"[yellow]Brak ofert wakacje.pl dla profilu {profile.name!r} w bazie — "
            f"uruchom najpierw `hs search {profile.name}` albo `hs monitor {profile.name}`.[/]"
        )
        return

    console.print(
        f"[bold]weryfikuj[/]: {profile.name} — sprawdzam {len(offers)} ofert "
        f"dla {profile.adults} os., odstęp {args.delay}s…"
    )

    verifier = PriceVerifier(delay=args.delay)
    results = []
    for offer in offers:
        v = verifier.verify(offer, adults=profile.adults)
        save_verification(store.db, v)
        results.append(v)

    t = Table(title=f"Weryfikacja ceny końcowej — {profile.name}",
              header_style="bold", show_lines=False)
    t.add_column("Hotel", max_width=30, overflow="ellipsis")
    t.add_column("Termin", width=13)
    t.add_column("N", justify="right", width=2)
    t.add_column("Listing", justify="right", width=8)
    t.add_column("Końcowa", justify="right", width=8)
    t.add_column("Różnica", justify="right", width=18)
    t.add_column("Uwagi", max_width=40, overflow="fold")

    for v in results:
        termin = f"{v.departure_date}" if v.departure_date else "—"
        if v.ok:
            final = _money(v.final_price)
        else:
            final = "[dim]—[/]"
        note = v.note or ""
        if not v.ok:
            note = f"[dim]nie udało się zweryfikować: {note}[/]"
        t.add_row(v.hotel_name or "—", termin, str(v.nights or "—"),
                  _money(v.listing_price), final, _diff_cell(v), note)
    console.print(t)

    checked = [v for v in results if v.ok and v.diff_pct is not None]
    failed = [v for v in results if not v.ok]
    agree = [v for v in checked if v.verdict == "zgodna"]
    stale = [v for v in checked if v.verdict == "nieaktualna"]
    drift = [v for v in checked if v.verdict == "odchylenie"]
    inflated = [v for v in checked if v.verdict == "zawyzona"]

    console.print()
    console.print(f"[bold]Podsumowanie[/] ({len(results)} ofert):")
    console.print(f"  [green]zgodna cena[/] (±{TOLERANCE_PCT:.0f}%): "
                  f"[bold]{len(agree)}[/]")
    console.print(f"  [yellow]odchylenie[/] ({TOLERANCE_PCT:.0f}–{SUSPICIOUS_PCT:.0f}%): "
                  f"[bold]{len(drift)}[/]")
    console.print(f"  [red]cena zawyżona[/] (>{SUSPICIOUS_PCT:.0f}%): "
                  f"[bold]{len(inflated)}[/]")
    if stale:
        console.print(
            f"  [cyan]cena zmieniła się od ostatniego przebiegu[/]: [bold]{len(stale)}[/] "
            f"[dim](listing zgadza się z kalkulatorem — to nasz snapshot jest stary)[/]"
        )
    if failed:
        console.print(f"  [dim]nie udało się zweryfikować: [bold]{len(failed)}[/][/]")

    # Cena „od" może być prawdziwa, a mimo to myląca: najtańszy pokój bywa
    # dużo gorszy niż ten, który człowiek faktycznie wybierze. To osobna
    # informacja niż „listing kłamie" i warto ją podać wprost.
    spread = [v for v in checked
              if v.max_price and v.final_price and v.max_price > v.final_price * 1.15]
    if spread:
        console.print(
            f"\n[dim]{len(spread)} ofert ma pokoje droższe o ponad 15% od "
            f"najtańszego wariantu — cena „od\" jest prawdziwa, ale dotyczy "
            f"najsłabszego pokoju.[/]"
        )
    if checked and not inflated and not drift and not stale:
        console.print("\n[green]Cena z listingu broni się na całej próbce — "
                      "ranking cenowy stoi na twardym gruncie.[/]")


def register(sub) -> None:
    p = sub.add_parser(
        "weryfikuj",
        help="sprawdź, czy cena z listingu to cena końcowa (kalkulator wakacje.pl)",
    )
    p.add_argument("profile")
    p.add_argument("--top", type=int, default=8,
                   help="ile najtańszych ofert profilu zweryfikować")
    p.add_argument("--delay", type=float, default=1.5,
                   help="minimalny odstęp między zapytaniami [s]")
    p.set_defaults(func=cmd_verify)
