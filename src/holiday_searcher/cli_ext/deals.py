"""CLI: hs diff | monitor — faza 2 (detekcja okazji + powiadomienia)."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .. import deals, notify
from ..cli import load_profile
from ..paths import DB_PATH
from ..providers.wakacje import WakacjeProvider
from ..storage import Storage

console = Console()


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def cmd_diff(args):
    profile = load_profile(args.profile)  # rzuci czytelny błąd, gdy nazwa się nie zgadza
    store = Storage(DB_PATH)
    diff = deals.diff_between_runs(store.db, profile.name)

    if diff is None:
        console.print(
            f"[yellow]Za mało przebiegów dla profilu {profile.name!r} — "
            f"potrzeba co najmniej dwóch (`hs search` lub `hs monitor`).[/]"
        )
        return

    if not diff.price_changes and not diff.new_offers and not diff.disappeared:
        console.print(f"[green]Brak zmian między dwoma ostatnimi przebiegami {profile.name!r}.[/]")
        return

    if diff.price_changes:
        t = Table(title=f"Zmiany cen — {profile.name}", header_style="bold", show_lines=False)
        t.add_column("Hotel", max_width=32, overflow="ellipsis")
        t.add_column("Miejsce", max_width=20, overflow="ellipsis")
        t.add_column("Stara cena", justify="right")
        t.add_column("Nowa cena", justify="right")
        t.add_column("Zmiana", justify="right")
        t.add_column("Kierunek", justify="center")
        t.add_column("URL", overflow="fold")
        for e in diff.price_changes:
            if e.event_type == "PRICE_DROP":
                direction = "[green]↓ taniej[/]"
            else:
                direction = "[red]↑ drożej[/]"
            t.add_row(
                e.hotel_name, f"{e.region} / {e.city}",
                _money(e.price_old or 0), _money(e.price_new),
                f"{e.pct_change:+.1f}%", direction,
                f"[dim blue]{e.url}[/]",
            )
        console.print(t)
    else:
        console.print("[dim]Brak zmian cen u ofert obecnych w obu przebiegach.[/]")

    if diff.new_offers:
        console.print(f"\n[bold green]Nowe oferty ({len(diff.new_offers)}):[/]")
        for e in diff.new_offers:
            console.print(f"  + {e.hotel_name} ({e.region} / {e.city}) — {_money(e.price_new)} zł/os")
            console.print(f"    [dim blue]{e.url}[/]")

    if diff.disappeared:
        console.print(f"\n[bold red]Zniknęły ({len(diff.disappeared)}):[/]")
        # Najpierw te, które zniknęły PO obniżce — to jedyne zniknięcia,
        # które coś znaczą (patrz deals.notifiable).
        for d in sorted(diff.disappeared, key=lambda d: not d.get("after_drop")):
            mark = "[bold yellow] ← zniknęła po obniżce[/]" if d.get("after_drop") else ""
            pct = f" ({d['pct_change']:+.1f}%)" if d.get("pct_change") is not None else ""
            console.print(
                f"  - {d['hotel_name']} ({d['region']} / {d['city']}) — "
                f"była {_money(d['price'])} zł/os{pct}{mark}"
            )


def cmd_monitor(args):
    profile = load_profile(args.profile)
    prov = WakacjeProvider(delay=args.delay)
    store = Storage(DB_PATH)

    console.print(f"[bold]monitor[/]: {profile.name} — pobieram do {args.limit} ofert…")
    run_id = store.start_run(profile.name, prov.name)
    offers = prov.search(profile, limit=args.limit)
    new, saved = store.save(offers, run_id)
    store.finish_run(run_id, len(offers), f"monitor limit={args.limit}")
    console.print(f"Zapisano [bold]{len(offers)}[/] ofert ([green]{new} nowych[/], {saved} snapshotów cen).")

    if not offers:
        console.print("[yellow]Brak ofert w tym przebiegu — pomijam detekcję zdarzeń.[/]")
        return

    keys = [o.key for o in offers]
    events = deals.scan_for_events(store.db, offer_keys=keys, drop_pct=args.drop_pct,
                                   profile=profile.name)
    to_send = deals.notifiable(store.db, events, cooldown_days=args.cooldown_days)

    by_type: dict[str, int] = {}
    for e in events:
        by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
    console.print(f"Zdarzenia: [bold]{len(events)}[/] wykrytych"
                  + (f" ({breakdown})" if breakdown else "") + ", "
                  f"[bold]{len(to_send)}[/] do wysłania po odfiltrowaniu anti-spamu.")

    # Zniknięcia bez wcześniejszej obniżki nie idą na Telegram (deals.notifiable),
    # ale w logu monitora mają się pojawić — inaczej informacja ginie bez śladu.
    quiet_vanished = [e for e in events
                      if e.event_type == "OFFER_VANISHED" and not e.is_sellout_signal]
    if quiet_vanished:
        console.print(f"[dim]Zniknęło z wyników bez obniżki ({len(quiet_vanished)}) "
                      f"— tylko do wiadomości, bez powiadomienia:[/]")
        for e in quiet_vanished[:10]:
            console.print(f"  [dim]· {e.hotel_name} ({e.region}) — "
                          f"ostatnio {_money(e.price_new)} zł/os[/]")
        if len(quiet_vanished) > 10:
            console.print(f"  [dim]… i {len(quiet_vanished) - 10} więcej[/]")

    if not to_send:
        console.print("[dim]Brak nowych okazji do zgłoszenia.[/]")
        return

    notifier = notify.TelegramNotifier()

    def _dispatch(text: str, covered: list, label: str) -> None:
        if args.dry_run:
            console.print(f"\n[dim]--- DRY RUN: {label} (nie wysłano, cooldown nie zużyty) ---[/]")
            console.print(text)
            return
        result = notifier.send(text)
        status = "[green]OK[/]" if result.ok else "[red]BŁĄD[/]"
        extra = f" ({result.detail})" if result.detail else ""
        console.print(f"{status} {label} — kanał: {result.channel}{extra}")
        # Cooldown zużywamy tylko po realnym dostarczeniu na Telegram; fallback
        # konsolowy jest echem do logu i MA się powtarzać, dopóki bot nie działa.
        if result.ok and result.channel == "telegram":
            for ev in covered:
                deals.mark_sent(store.db, ev)

    # Kolejność jest celowa: najmocniejszy sygnał (rekord historii) idzie
    # pierwszy, nowości — jako ostatnie, bo są najbardziej hałaśliwe.
    floors = [e for e in to_send if e.event_type == "PRICE_FLOOR"]
    drops = [e for e in to_send if e.event_type == "PRICE_DROP"]
    vanished = [e for e in to_send if e.event_type == "OFFER_VANISHED"]
    news = [e for e in to_send if e.event_type == "NEW_OFFER"]

    for event in floors + drops:
        _dispatch(notify.format_event(event), [event],
                  f"{event.event_type} — {event.hotel_name}")
    if vanished:
        _dispatch(notify.format_vanished_digest(vanished, profile.name), vanished,
                  f"OFFER_VANISHED ({len(vanished)} ofert po obniżce)")
    if news:
        _dispatch(notify.format_new_offers_digest(news, profile.name), news,
                  f"NEW_OFFER digest ({len(news)} ofert)")


def register(sub) -> None:
    d = sub.add_parser("diff", help="różnice cen między dwoma ostatnimi przebiegami profilu")
    d.add_argument("profile")
    d.set_defaults(func=cmd_diff)

    m = sub.add_parser("monitor", help="pobierz, wykryj okazje i wyślij powiadomienia (dla launchd)")
    m.add_argument("profile")
    m.add_argument("--limit", type=int, default=150, help="ile ofert pobrać")
    m.add_argument("--delay", type=float, default=1.5, help="przerwa między stronami [s]")
    m.add_argument("--drop-pct", type=float, default=deals.DROP_PCT_DEFAULT,
                   help="minimalny spadek %% uznawany za okazję")
    m.add_argument("--cooldown-days", type=int, default=deals.COOLDOWN_DAYS_DEFAULT,
                   help="ile dni odczekać przed ponownym powiadomieniem o tym samym zdarzeniu")
    m.add_argument("--dry-run", action="store_true",
                   help="tylko wypisz wiadomości, nie wysyłaj i nie zużywaj cooldownu")
    m.set_defaults(func=cmd_monitor)
