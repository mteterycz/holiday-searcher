"""CLI: hs pilnuj dodaj|lista|usun|sprawdz — watchlist konkretnych hoteli.

Wzorowane na cli_ext/deals.py (`diff`/`monitor`), ale pilnowanie działa na
poziomie HOTELU, nie profilu: hotel ma zostać na radarze nawet gdy jego cena
chwilowo wyskoczy ponad `max_price_pp` profilu."""
from __future__ import annotations

import time

import yaml
from rich.console import Console
from rich.table import Table

from .. import notify, watchlist
from ..cli import load_profile
from ..paths import CONFIG_DIR, DB_PATH
from ..providers.wakacje import WakacjeProvider
from ..storage import Storage

console = Console()


def _money(n) -> str:
    return "-" if n is None else f"{n:,}".replace(",", " ")


def _first_profile_name() -> str:
    data = yaml.safe_load((CONFIG_DIR / "profiles.yaml").read_text(encoding="utf-8"))
    profiles = data.get("profiles") or []
    if not profiles:
        raise SystemExit("Brak profili w config/profiles.yaml — podaj --profil ręcznie.")
    return profiles[0]["name"]


def _resolve_profile_name(explicit: str | None) -> str:
    return explicit or _first_profile_name()


# ---------- dodaj ----------

def cmd_dodaj(args):
    store = Storage(DB_PATH)
    matches = watchlist.find_matches(store.db, args.hotel)
    if not matches:
        console.print(
            f"[red]Nie znaleziono w bazie hotelu pasującego do {args.hotel!r} "
            f"(hotel_id albo fragment nazwy). Uruchom najpierw `hs search`, "
            f"żeby hotel trafił do bazy.[/]"
        )
        return
    if len(matches) > 1:
        console.print(f"[yellow]Wiele hoteli pasuje do {args.hotel!r} — doprecyzuj (użyj hotel_id):[/]")
        t = Table(header_style="bold")
        t.add_column("hotel_id")
        t.add_column("Hotel")
        t.add_column("Kraj")
        t.add_column("Region")
        for m in matches:
            t.add_row(m["hotel_id"], m["hotel_name"], m["country"] or "-", m["region"] or "-")
        console.print(t)
        return

    m = matches[0]
    profile_name = _resolve_profile_name(args.profil)
    load_profile(profile_name)  # rzuci czytelny błąd, gdy profil nie istnieje

    watch_id = watchlist.add_watch(
        store.db, hotel_id=m["hotel_id"], hotel_name=m["hotel_name"], provider=m["provider"],
        profile=profile_name, target_price_pp=args.cel, note=args.notatka,
    )
    extra = f", cel: {_money(args.cel)} zł/os" if args.cel else ""
    console.print(
        f"[green]Dodano do watchlisty[/] #{watch_id}: {m['hotel_name']} "
        f"({m['country'] or '-'} / {m['region'] or '-'}), profil: {profile_name}{extra}"
    )


# ---------- lista ----------

def cmd_lista(args):
    store = Storage(DB_PATH)
    rows = watchlist.list_active(store.db)
    if not rows:
        console.print("[dim]Watchlista jest pusta — dodaj hotel: `hs pilnuj dodaj <hotel_id_lub_nazwa>`.[/]")
        return

    t = Table(title="Watchlista hoteli", header_style="bold", show_lines=False)
    t.add_column("#", justify="right", width=3)
    t.add_column("Hotel", max_width=30, overflow="ellipsis")
    t.add_column("Kraj/region", max_width=24, overflow="ellipsis")
    t.add_column("Cel", justify="right")
    t.add_column("Aktualna min.", justify="right")
    t.add_column("Różnica do celu", justify="right")
    t.add_column("Notatka", max_width=20, overflow="ellipsis")
    t.add_column("Od kiedy")

    for r in rows:
        country, region = watchlist.hotel_location(store.db, r["hotel_id"], r["provider"])
        best = watchlist.current_best_price(store.db, r["hotel_id"], r["provider"])
        target = r["target_price_pp"]
        if best is not None and target:
            diff = best - target
            style = "green" if diff <= 0 else "red"
            diff_s = f"[{style}]{diff:+,}[/]".replace(",", " ")
        else:
            diff_s = "-"
        place = f"{country} / {region}".strip(" /") or "-"
        t.add_row(
            str(r["id"]), r["hotel_name"], place,
            _money(target), _money(best), diff_s,
            r["note"] or "-", (r["added_at"] or "")[:10],
        )
    console.print(t)


# ---------- usun ----------

def cmd_usun(args):
    store = Storage(DB_PATH)
    rows = watchlist.find_active_by_id_or_fragment(store.db, args.watch)
    if not rows:
        console.print(f"[yellow]Brak aktywnych wpisów pasujących do {args.watch!r}.[/]")
        return
    if len(rows) > 1:
        console.print(f"[yellow]Wiele wpisów pasuje do {args.watch!r} — doprecyzuj (podaj ID):[/]")
        for r in rows:
            console.print(f"  #{r['id']} {r['hotel_name']}")
        return
    r = rows[0]
    watchlist.deactivate(store.db, r["id"])
    console.print(f"[green]Wyłączono pilnowanie[/] #{r['id']}: {r['hotel_name']} (historia zachowana).")


# ---------- sprawdz ----------

def cmd_sprawdz(args):
    store = Storage(DB_PATH)
    rows = watchlist.list_active(store.db)
    if not rows:
        console.print("[dim]Watchlista jest pusta.[/]")
        return

    delay = max(args.delay, 1.5)
    prov = WakacjeProvider(delay=delay)
    notifier = notify.TelegramNotifier()

    total_events = 0
    total_sent = 0

    for i, r in enumerate(rows):
        if i:
            time.sleep(delay)
        profile_name = r["profile"] or _resolve_profile_name(None)
        try:
            profile = load_profile(profile_name)
        except SystemExit as exc:
            console.print(f"[red]#{r['id']} {r['hotel_name']}: {exc}[/]")
            continue

        console.print(f"[dim]Sprawdzam #{r['id']} {r['hotel_name']}…[/]")
        try:
            events = watchlist.check_entry(store, prov, r, profile)
        except RuntimeError as exc:
            console.print(f"[red]  błąd pobierania: {exc}[/]")
            continue

        if not events:
            console.print("  brak zdarzeń")
            continue
        total_events += len(events)

        to_send = events if args.dry_run else watchlist.notifiable(
            store.db, events, cooldown_days=args.cooldown_days)
        if not to_send:
            console.print(f"  {len(events)} zdarzeń, ale w cooldownie — pomijam wysyłkę")
            continue

        for ev in to_send:
            text = watchlist.format_watch_event(ev)
            if args.dry_run:
                console.print(f"\n[dim]--- DRY RUN: {ev.event_type} — {ev.hotel_name} "
                              f"(nie wysłano, cooldown nie zużyty) ---[/]")
                console.print(text)
                continue
            result = notifier.send(text)
            status = "[green]OK[/]" if result.ok else "[red]BŁĄD[/]"
            extra = f" ({result.detail})" if result.detail else ""
            console.print(f"  {status} {ev.event_type} — kanał: {result.channel}{extra}")
            if result.ok and result.channel == "telegram":
                watchlist.mark_sent(store.db, ev)
                total_sent += 1

    console.print(
        f"\nRazem: [bold]{total_events}[/] zdarzeń"
        + ("" if args.dry_run else f", [bold]{total_sent}[/] wysłanych")
    )


def register(sub) -> None:
    p = sub.add_parser("pilnuj", help="watchlist konkretnych hoteli (niezależnie od filtrów profilu)")
    watch_sub = p.add_subparsers(dest="pilnuj_cmd", required=True)

    d = watch_sub.add_parser("dodaj", help="dodaj hotel do watchlisty")
    d.add_argument("hotel", help="hotel_id albo fragment nazwy hotelu")
    d.add_argument("--cel", type=int, default=None, help="docelowa cena za osobę [zł]")
    d.add_argument("--profil", default=None,
                   help="profil wyszukiwania (domyślnie: pierwszy z config/profiles.yaml)")
    d.add_argument("--notatka", default=None, help="dowolna notatka")
    d.set_defaults(func=cmd_dodaj)

    l = watch_sub.add_parser("lista", help="pokaż watchlistę")
    l.set_defaults(func=cmd_lista)

    u = watch_sub.add_parser("usun", help="dezaktywuj wpis watchlisty (historia zostaje)")
    u.add_argument("watch", help="ID wpisu (z `hs pilnuj lista`) albo fragment nazwy hotelu")
    u.set_defaults(func=cmd_usun)

    s = watch_sub.add_parser("sprawdz", help="sprawdź pilnowane hotele i wyślij alerty")
    s.add_argument("--dry-run", action="store_true",
                   help="tylko wypisz wiadomości, nie wysyłaj i nie zużywaj cooldownu")
    s.add_argument("--delay", type=float, default=1.5, help="przerwa między zapytaniami [s]")
    s.add_argument("--cooldown-days", type=int, default=watchlist.DEFAULT_COOLDOWN_DAYS,
                   help="ile dni odczekać przed ponownym powiadomieniem o tym samym (hotel, zdarzenie)")
    s.set_defaults(func=cmd_sprawdz)
