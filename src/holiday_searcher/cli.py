"""CLI: hs count | search | top | stats"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from .curate import curate, summarize
from .models import Destination, SearchProfile
from .providers.wakacje import SORT_POPULAR, WakacjeProvider
from .scoring import score_all
from .storage import Storage

console = Console()
ROOT = Path(__file__).resolve().parents[2]


def load_profile(name: str, path: Path | None = None) -> SearchProfile:
    path = path or ROOT / "config" / "profiles.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for p in data.get("profiles", []):
        if p["name"] == name:
            return SearchProfile(
                name=p["name"],
                country=p.get("country", ""),
                date_from=_as_date(p["date_from"]),
                date_to=_as_date(p["date_to"]),
                nights_min=int(p["nights_min"]),
                nights_max=int(p["nights_max"]),
                boards=list(p.get("boards") or []),
                adults=int(p.get("adults", 2)),
                children_ages=list(p.get("children_ages") or []),
                stars_min=int(p.get("stars_min") or 0),
                rating_min=float(p.get("rating_min") or 0),
                max_price_pp=p.get("max_price_pp"),
                departures=list(p.get("departures") or []),
                regions=[str(r) for r in (p.get("regions") or [])],
                vibe=p.get("vibe"),
                destinations=[
                    Destination(
                        country=d["country"],
                        regions=[str(r) for r in (d.get("regions") or [])],
                        boards=list(d.get("boards") or []),
                        label=d.get("label") or d["country"].capitalize(),
                        max_price_pp=d.get("max_price_pp"),
                    )
                    for d in (p.get("destinations") or [])
                ],
            )
    available = ", ".join(p["name"] for p in data.get("profiles", []))
    raise SystemExit(f"Nie ma profilu {name!r}. Dostępne: {available}")


def _as_date(v) -> date:
    return v if isinstance(v, date) else datetime.strptime(str(v), "%Y-%m-%d").date()


def _table(scored, limit: int, title: str) -> Table:
    t = Table(title=title, header_style="bold", show_lines=False)
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Ocena", justify="right", width=5)
    t.add_column("Hotel", max_width=34, overflow="ellipsis")
    t.add_column("Miejsce", max_width=20, overflow="ellipsis")
    t.add_column("★", justify="center", width=3)
    t.add_column("Opinie", justify="right", width=6)
    t.add_column("Termin", width=13)
    t.add_column("N", justify="right", width=2)
    t.add_column("Wyżywienie", max_width=17, overflow="ellipsis")
    t.add_column("Wylot", max_width=11, overflow="ellipsis")
    t.add_column("Cena/os", justify="right", width=8)
    t.add_column("zł/os/noc", justify="right", width=9)
    t.add_column("vs koszyk", justify="right", width=9)
    t.add_column("Biuro", max_width=14, overflow="ellipsis")

    for i, s in enumerate(scored[:limit], 1):
        o = s.offer
        rel = f"{s.price_index:.2f}×"
        rel_style = "green" if s.price_index >= 1.10 else ("red" if s.price_index < 0.92 else "")
        t.add_row(
            str(i), f"{s.score:.1f}", o.hotel_name, f"{o.region} / {o.city}",
            f"{o.stars:.0f}" if o.stars else "-",
            f"{o.rating:.1f}" if o.rating else "-",
            f"{o.departure_date:%d.%m}–{o.return_date:%d.%m}", str(o.nights),
            o.board_raw or o.board, o.departure_place,
            f"{o.price:,}".replace(",", " "),
            f"{o.price_ppn:.0f}",
            f"[{rel_style}]{rel}[/]" if rel_style else rel,
            o.tour_operator,
        )
    return t


def cmd_count(args):
    p = load_profile(args.profile)
    n = WakacjeProvider().count(p)
    console.print(f"[bold]{p.name}[/]: {n} ofert pasuje do filtrów")


def cmd_search(args):
    p = load_profile(args.profile)
    prov = WakacjeProvider(delay=args.delay)
    store = Storage(ROOT / "data" / "offers.db")

    legs = p.legs()
    console.print(f"[bold]{p.name}[/] — {p.date_from}…{p.date_to}, "
                  f"{p.nights_min}-{p.nights_max} nocy, {p.adults} os."
                  + (f", ocena ≥ {p.rating_min}" if p.rating_min else ""))
    console.print("Kierunki: " + ", ".join(
        f"{l.name} ({'/'.join(l.boards) or 'dowolne'})" for l in legs))

    counts = prov.counts_by_leg(p)
    total = sum(counts.values())
    console.print("W serwisie: " + ", ".join(f"{k}: [bold]{v}[/]" for k, v in counts.items())
                  + f" — razem {total}. Pobieram do {args.limit}…")

    run_id = store.start_run(p.name, prov.name)
    offers = prov.search(p, limit=args.limit)

    # Próbka referencyjna: sortowanie po popularności, nie po cenie. Służy WYŁĄCZNIE
    # do liczenia median koszyków — inaczej porównywalibyśmy tani ogon sam ze sobą.
    reference = []
    if args.reference:
        console.print(f"Próbka referencyjna do median koszyków ({args.reference})…")
        reference = prov.search_reference(p, limit=args.reference)

    if not offers:
        store.finish_run(run_id, 0, "brak wyników")
        console.print("[yellow]Brak wyników — poluzuj filtry w config/profiles.yaml[/]")
        return

    scored_all = score_all(offers, reference=reference or None)

    # Do bazy trafia tylko kuracja: najlepszy wariant hotelu, limit na kraj.
    # Reszta była materiałem statystycznym i po policzeniu median jest zbędna.
    scored = curate(scored_all, per_country=args.keep_per_country)
    console.print("[dim]" + summarize(scored_all, scored) + "[/]")

    new, saved = store.save([s.offer for s in scored], run_id)
    store.finish_run(run_id, len(scored), f"pobrano={len(offers)} total={total}")

    console.print(f"Zapisano [bold]{len(scored)}[/] ofert "
                  f"([green]{new} nowych[/], {saved} snapshotów cen)"
                  + (f", referencja: {len(reference)}" if reference else "") + "\n")
    console.print(_table(scored, args.top, f"TOP {args.top} — {p.name}"))
    console.print("\n[dim]Ocena 0-10: 45% cena vs koszyk, 30% jakość, "
                  "10% wyżywienie, 15% promocja.\n"
                  "Koszyk = ten sam region, ta sama kategoria ★, ta sama rodzina wyżywienia.[/]")
    for i, s in enumerate(scored[:3], 1):
        console.print(f"[dim]{i}. {s.offer.hotel_name}: {s.explain()}[/]")
        console.print(f"   [dim blue]{s.offer.url}[/]")


def cmd_stats(args):
    st = Storage(ROOT / "data" / "offers.db").stats()
    console.print(f"Oferty: [bold]{st['offers']}[/] | "
                  f"Snapshoty cen: [bold]{st['snapshots']}[/] | "
                  f"Przebiegi: [bold]{st['runs']}[/]")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hs", description="Wyszukiwarka ofert wakacyjnych")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("count", help="ile ofert pasuje do profilu")
    c.add_argument("profile")
    c.set_defaults(func=cmd_count)

    s = sub.add_parser("search", help="pobierz, zapisz i oceń oferty")
    s.add_argument("profile")
    s.add_argument("--limit", type=int, default=120, help="ile ofert pobrać")
    s.add_argument("--top", type=int, default=15, help="ile pokazać")
    s.add_argument("--delay", type=float, default=1.5, help="przerwa między stronami [s]")
    s.add_argument("--keep-per-country", type=int, default=15,
                   help="ile najlepszych ofert na kraj utrwalić w bazie")
    s.add_argument("--reference", type=int, default=300,
                   help="rozmiar nieobciążonej próbki do median koszyków (0 = wyłącz)")
    s.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="stan bazy")
    st.set_defaults(func=cmd_stats)

    from . import cli_ext
    cli_ext.register_all(sub)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
