"""CLI: hs kalendarz — siatka „cena vs data wylotu × liczba nocy".

Odpowiada na pytanie, którego lista TOP-15 nie potrafi zadać: czy warto
przesunąć wylot o parę dni albo skrócić pobyt o jedną noc.
"""
from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.table import Table

from .. import price_calendar as pc
from ..cli import load_profile
from ..paths import DB_PATH
from ..providers.wakacje import WakacjeProvider

console = Console()

# Style komórek. Minimum jest jedno (liczone w zł/os/noc — jedynej wielkości
# porównywalnej między kolumnami o różnej liczbie nocy), „blisko minimum"
# to wszystko w granicach 5%.
STYLE_MIN = "bold green"
STYLE_NEAR = "green"
STYLE_COL_MIN = "bold"      # najtańszy termin w obrębie jednej kolumny


def _grid_table(grid, profile, title: str) -> Table:
    dates = sorted({k[0] for k in grid})
    nights = sorted({k[1] for k in grid})

    best = pc.best_cell(grid)
    near = pc.near_minimum_keys(grid)
    col_min = pc.column_minimums(grid)

    t = Table(title=title, header_style="bold", show_lines=False, pad_edge=False)
    t.add_column("Wylot", justify="left", width=9)
    for n in nights:
        t.add_column(f"{n} nocy", justify="right", width=9)

    for day in dates:
        inside = pc.in_profile_window(profile, day)
        # Terminy spoza okna profilu są przygaszone — to one są „bonusem"
        # z parametru --spread i użytkownik musi widzieć, że to rozszerzenie.
        label = f"{day:%d.%m} {_weekday(day)}"
        cells = [label if inside else f"[dim]{label}[/]"]
        for n in nights:
            cell = grid.get((day, n))
            if cell is None:
                cells.append("[dim]·[/]")
                continue
            text = f"{pc.money(cell.price_pp)}\n[dim]{pc.money(cell.price_ppn)}/n[/]"
            if best is not None and cell.key == best.key:
                style = STYLE_MIN
            elif cell.key in near:
                style = STYLE_NEAR
            elif col_min.get(n) is not None and col_min[n].key == cell.key:
                style = STYLE_COL_MIN
            else:
                style = ""
            cells.append(f"[{style}]{text}[/]" if style else text)
        t.add_row(*cells)
    return t


_WEEKDAYS = ["pn", "wt", "śr", "cz", "pt", "sb", "nd"]


def _weekday(d: date) -> str:
    return _WEEKDAYS[d.weekday()]


def cmd_calendar(args):
    profile = load_profile(args.profile)
    delay = max(pc.MIN_DELAY, args.delay)
    if args.delay < pc.MIN_DELAY:
        console.print(f"[yellow]--delay podniesiony do {pc.MIN_DELAY}s — nie zasypujemy API.[/]")

    dates = pc.departure_dates(profile, spread=args.spread,
                               max_dates=args.max_dates, today=date.today())
    if not dates:
        console.print("[yellow]Okno dat jest puste (całe w przeszłości?) — nic do sprawdzenia.[/]")
        return

    scope = f"hotel {args.hotel}" if args.hotel else f"{len(profile.legs())} kierunków"
    console.print(
        f"[bold]kalendarz cen[/]: {profile.name} — {scope}\n"
        f"Okno profilu {profile.date_from:%d.%m}–{profile.date_to:%d.%m}, "
        f"±{args.spread} dni → {len(dates)} dat wylotu "
        f"({dates[0]:%d.%m}–{dates[-1]:%d.%m}), {profile.nights_min}–{profile.nights_max} nocy, "
        f"limit {args.limit}, przerwa {delay}s."
    )

    def progress(day: date, found: int) -> None:
        console.print(f"  [dim]{day:%d.%m}: {found} ofert[/]")

    if args.hotel:
        client = pc.HotelCalendarClient(delay=delay)
        try:
            offers = pc.collect_hotel(client, profile, dates, args.hotel,
                                      limit=args.limit, delay=delay, progress=progress)
        finally:
            client.close()
    else:
        prov = WakacjeProvider(delay=delay)
        offers = pc.collect_profile(prov, profile, dates, limit=args.limit,
                                    delay=delay, progress=progress)

    grid = pc.aggregate(offers, dates=dates,
                        nights_range=(profile.nights_min, profile.nights_max))
    if not grid:
        console.print("[yellow]Żadna kombinacja daty i długości pobytu nie zwróciła oferty — "
                      "poluzuj filtry profilu albo zwiększ --limit.[/]")
        return

    title = f"Kalendarz cen — {profile.name}" + (f" / hotel {args.hotel}" if args.hotel else "")
    console.print()
    console.print(_grid_table(grid, profile, title))
    # Uwaga: nawiasy kwadratowe to składnia znaczników rich — w tekście dla
    # użytkownika piszemy jednostki słowami, żeby nie znikały po parsowaniu.
    console.print("[dim]Górna liczba: najtańsza cena za osobę w zł. Dolna: ta sama oferta "
                  "w zł/os/noc.\n"
                  f"[{STYLE_MIN}]zielone[/] = minimum (w zł/os/noc), "
                  f"[{STYLE_NEAR}]jasnozielone[/] = do 5% od minimum, "
                  f"[{STYLE_COL_MIN}]pogrubione[/] = najtańszy termin w tej kolumnie. "
                  "Wyszarzone daty leżą poza oknem profilu.[/]")

    console.print(f"\n[bold]{pc.summarize(grid, profile)}[/]")

    rows = pc.spread_report(grid)
    if rows:
        console.print("\n[bold]Ile daje samo przesunięcie daty (ta sama długość pobytu):[/]")
        for nights, cheap, pricey, pct in rows:
            console.print(
                f"  {nights} nocy: {cheap.departure_date:%d.%m} "
                f"[green]{pc.money(cheap.price_pp)} zł[/] vs "
                f"{pricey.departure_date:%d.%m} [red]{pc.money(pricey.price_pp)} zł[/] "
                f"→ różnica {pc.money(pricey.price_pp - cheap.price_pp)} zł/os ({pct:.0f}%)"
            )

    best = pc.best_cell(grid)
    if best is not None and best.url:
        console.print(f"\n[dim blue]{best.url}[/]")

    if args.no_save:
        console.print("\n[dim]--no-save: nie zapisuję do bazy.[/]")
        return
    stamp = pc.save_calendar(DB_PATH, profile.name, grid,
                             hotel_id=args.hotel or pc.ALL_HOTELS)
    console.print(f"\n[dim]Zapisano {len(grid)} komórek do price_calendar (checked_at={stamp}).[/]")


def register(sub) -> None:
    p = sub.add_parser("kalendarz",
                       help="siatka cen: data wylotu × liczba nocy (gdzie taniej o kilka dni)")
    p.add_argument("profile")
    p.add_argument("--spread", type=int, default=pc.DEFAULT_SPREAD,
                   help="ile dni przed i po oknie profilu dołożyć (domyślnie 5)")
    p.add_argument("--hotel", default=None, metavar="ID",
                   help="kalendarz dla jednego hotelu (hotel_id z bazy/adresu oferty)")
    p.add_argument("--limit", type=int, default=pc.DEFAULT_LIMIT,
                   help="ile ofert pobierać na jedną datę wylotu (tryb --hotel: na cały kalendarz)")
    p.add_argument("--delay", type=float, default=pc.MIN_DELAY,
                   help="przerwa między zapytaniami [s], nie mniej niż 1.5")
    p.add_argument("--max-dates", type=int, default=pc.DEFAULT_MAX_DATES,
                   help="twardy sufit liczby sprawdzanych dat (bezpiecznik kosztu zapytań)")
    p.add_argument("--no-save", action="store_true", help="nie zapisuj wyniku do bazy")
    p.set_defaults(func=cmd_calendar)
