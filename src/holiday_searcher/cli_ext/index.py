"""CLI: hs indeks — pozycja bieżącej ceny na tle własnej historii hotelu.

Uzupełnienie kolumny „vs koszyk" z `hs search`: tam hotel jest porównywany z
rynkiem, tutaj — sam ze sobą. Tabela świadomie pokazuje też hotele bez
historii (z pewnością „brak"), zamiast je ukrywać: pusty ekran w pierwszych
dniach zbierania danych wyglądałby jak awaria, a nie jak brak pomiarów.
"""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .. import hotel_index
from ..cli import load_profile
from ..paths import DB_PATH
from ..storage import Storage

console = Console()

_CONF_STYLE = {
    hotel_index.CONF_HIGH: "green",
    hotel_index.CONF_MEDIUM: "cyan",
    hotel_index.CONF_LOW: "yellow",
    hotel_index.CONF_NONE: "dim",
}


def _money(n: int | float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def _span(days: float) -> str:
    """Rozpiętość historii. Minuty pokazujemy wprost, bo „0 h" przy dwóch
    przebiegach kwadrans po sobie wyglądało jak brak danych."""
    if days <= 0:
        return "—"
    hours = days * 24
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 24:
        return f"{hours:.0f} h"
    return f"{days:.1f} d"


def _samples(idx) -> str:
    """„snapshoty/momenty" — te liczby rozjeżdżają się, gdy hotel jest
    sprzedawany w kilku wariantach naraz, a to właśnie ta druga decyduje o
    tym, czy wolno mówić o historii (patrz HotelPriceIndex.reliable)."""
    if idx.time_points == idx.samples:
        return str(idx.samples)
    return f"{idx.samples}/{idx.time_points}"


def _position(idx) -> str:
    """Pozycja percentylowa — pokazywana tylko wtedy, gdy coś znaczy.

    Przy 1 pomiarze jest nieokreślona, przy 2-4 policzalna, ale mylącą
    precyzją sugerowałaby wiedzę, której nie mamy — stąd nawias."""
    if idx.percentile is None:
        return "[dim]—[/]"
    pct = f"{idx.percentile * 100:.0f}%"
    if not idx.reliable:
        return f"[dim]({pct})[/]"
    if idx.percentile <= hotel_index.BOTTOM_QUANTILE:
        return f"[bold green]{pct}[/]"
    if idx.percentile >= 0.8:
        return f"[red]{pct}[/]"
    return pct


def cmd_index(args):
    profile = load_profile(args.profile)   # walidacja nazwy profilu jak w innych komendach
    store = Storage(DB_PATH)

    rows = hotel_index.build_all(store.db, profile=profile.name)
    if not rows:
        console.print(
            f"[yellow]Brak danych cenowych dla profilu {profile.name!r} — "
            f"uruchom najpierw `hs search {profile.name}` albo `hs monitor {profile.name}`.[/]")
        return

    shown = rows if args.all else rows[:args.top]

    t = Table(title=f"Indeks cen własnych hoteli — {profile.name}",
              header_style="bold", show_lines=False)
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Hotel", min_width=14, max_width=30, overflow="ellipsis", no_wrap=True)
    t.add_column("Miejsce", min_width=9, max_width=18, overflow="ellipsis", no_wrap=True)
    t.add_column("Teraz", justify="right", no_wrap=True)
    t.add_column("Pakiet", justify="right", no_wrap=True)
    # Min/mediana/max w jednej kolumnie: trzy osobne rozsadzały tabelę na
    # węższych terminalach, a i tak czyta się je razem jako jeden zakres.
    t.add_column("Min·Med·Max", justify="center", no_wrap=True)
    t.add_column("Poz.", justify="right", no_wrap=True)
    t.add_column("n", justify="right", no_wrap=True)
    t.add_column("Okres", justify="right", no_wrap=True)
    t.add_column("Pewność", no_wrap=True)

    for i, idx in enumerate(shown, 1):
        name = idx.hotel_name
        if idx.at_historic_low:
            name = f"[bold green]▼ {name}[/]"
        elif idx.in_bottom_zone:
            name = f"[green]{name}[/]"
        conf_style = _CONF_STYLE.get(idx.confidence, "")
        history = (f"{idx.min_ppn:.0f} · {idx.median_ppn:.0f} · {idx.max_ppn:.0f}"
                   if idx.samples > 1 else "[dim]—[/]")
        t.add_row(
            str(i), name, f"{idx.region} / {idx.city}" if idx.city else idx.region,
            f"{idx.current_ppn:.0f}",
            f"{_money(idx.current_price)}",
            history,
            _position(idx),
            _samples(idx), _span(idx.span_days),
            f"[{conf_style}]{idx.confidence}[/]" if conf_style else idx.confidence,
        )
    console.print(t)

    console.print("\n[dim]Teraz / Min / Mediana / Max: zł za osobę za NOC — tylko w tej "
                  "jednostce wolno porównywać warianty tego samego hotelu.\n"
                  "Pakiet: bieżąca cena za osobę za cały wyjazd. "
                  "Poz.: percentyl bieżącej ceny we własnej historii (niżej = lepiej).\n"
                  "n: liczba snapshotów, a po ukośniku — liczba różnych MOMENTÓW pomiaru "
                  "(kilka wariantów hotelu w jednym przebiegu to wciąż jeden moment).\n"
                  f"[green]Zielony[/] = dolne {int(hotel_index.BOTTOM_QUANTILE * 100)}% "
                  f"własnej historii, [bold green]▼[/] = historyczne minimum "
                  f"(orzekane dopiero od {hotel_index.MIN_SAMPLES_FOR_CLAIM} "
                  f"momentów pomiaru).[/]")

    reliable = [r for r in rows if r.reliable]
    lows = [r for r in rows if r.at_historic_low]
    zone = [r for r in rows if r.in_bottom_zone]
    console.print(
        f"\nHoteli: [bold]{len(rows)}[/] | z historią ≥ "
        f"{hotel_index.MIN_SAMPLES_FOR_CLAIM} momentów pomiaru: [bold]{len(reliable)}[/] | "
        f"w dolnych {int(hotel_index.BOTTOM_QUANTILE * 100)}%: [bold]{len(zone)}[/] | "
        f"na historycznym minimum: [bold]{len(lows)}[/]")

    if not reliable:
        console.print(
            "[yellow]Żaden hotel nie ma jeszcze "
            f"{hotel_index.MIN_SAMPLES_FOR_CLAIM} momentów pomiaru — kolumny "
            "min/mediana/max opisują na razie pojedyncze obserwacje (albo rozrzut "
            "wariantów w jednym przebiegu), a nie historię cen. "
            "Indeks zacznie coś znaczyć po kilku przebiegach `hs monitor`.[/]")
    elif lows:
        console.print("[bold green]Na historycznym minimum:[/]")
        for idx in lows[:10]:
            console.print(f"  ▼ {idx.hotel_name} ({idx.region}) — {idx.headline()}")
            console.print(f"    [dim blue]{idx.url}[/]")


def register(sub) -> None:
    i = sub.add_parser("indeks", help="pozycja bieżącej ceny na tle własnej historii hotelu")
    i.add_argument("profile")
    i.add_argument("--top", type=int, default=20, help="ile hoteli pokazać")
    i.add_argument("--all", action="store_true", help="pokaż wszystkie, bez limitu --top")
    i.set_defaults(func=cmd_index)
