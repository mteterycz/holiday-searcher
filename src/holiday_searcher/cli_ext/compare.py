"""`hs compare <profil>` — ta sama Turcja u dwóch dostawców obok siebie.

Sens komendy: pokazać hotele dostępne w OBU źródłach i różnicę ceny. Żeby ta
różnica cokolwiek znaczyła, zestawiamy tylko oferty porównywalne — ta sama
rodzina wyżywienia i długość pobytu różniąca się najwyżej o jedną noc; z takich
par wybieramy najbliższą sobie terminem.

Komenda jest READ-ONLY wobec offers.db: nie zapisuje ofert ani snapshotów cen.
Jedyne, co utrwala, to tabela hotel_alias — wynik dopasowania hoteli.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from ..dedup import AliasStore, HotelMatch, hotels_from_offers, match_hotels
from ..models import Offer
from ..paths import DB_PATH
from ..providers.rpl import RplProvider
from ..providers.wakacje import WakacjeProvider

console = Console()

AI_FAMILY = {"AI", "UAI", "AI_PLUS", "AI_SOFT"}
MAX_NIGHTS_DIFF = 1


def board_family(board: str) -> str:
    """AI/UAI/AI Plus/AI Soft to jedna rodzina — inaczej nie porównalibyśmy nic,
    bo r.pl w ogóle nie rozróżnia wariantów all inclusive."""
    return "AI" if board in AI_FAMILY else (board or "OTHER")


@dataclass
class Pairing:
    match: HotelMatch
    left: Offer
    right: Offer

    @property
    def diff(self) -> int:
        return self.right.price - self.left.price

    @property
    def diff_pct(self) -> float:
        return (self.diff / self.left.price * 100) if self.left.price else 0.0


def _best_pair(left: list[Offer], right: list[Offer]) -> tuple[Offer, Offer] | None:
    """Najbliższe sobie oferty: ta sama rodzina wyżywienia, |Δnocy| <= 1,
    a spośród takich — najbliższe terminem, potem najtańsze."""
    best = None
    best_key = None
    for a in left:
        for b in right:
            if board_family(a.board) != board_family(b.board):
                continue
            nights_diff = abs(a.nights - b.nights)
            if nights_diff > MAX_NIGHTS_DIFF:
                continue
            date_diff = abs((a.departure_date - b.departure_date).days)
            key = (nights_diff, date_diff, a.price + b.price)
            if best_key is None or key < best_key:
                best_key, best = key, (a, b)
    return best


def _offers_by_hotel(offers: list[Offer]) -> dict[str, list[Offer]]:
    out: dict[str, list[Offer]] = {}
    for o in offers:
        out.setdefault(o.hotel_id or o.hotel_name, []).append(o)
    return out


def _fetch(provider, profile, limit: int) -> list[Offer]:
    try:
        offers = provider.search(profile, limit=limit)
        console.print(f"  {provider.name}: [bold]{len(offers)}[/] ofert")
        return offers
    except Exception as exc:                       # noqa: BLE001 — jedno źródło padło,
        console.print(f"  [red]{provider.name}: błąd — {exc}[/]")   # drugie ma działać
        return []


def _table(pairings: list[Pairing], name_a: str, name_b: str, limit: int) -> Table:
    t = Table(title=f"Hotele dostępne u obu dostawców ({len(pairings)})",
              header_style="bold", show_lines=False)
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Hotel", max_width=30, overflow="ellipsis")
    t.add_column("Region", max_width=18, overflow="ellipsis")
    t.add_column("★", justify="center", width=3)
    t.add_column(f"Termin / nocy / wyż. ({name_a})", max_width=26, overflow="ellipsis")
    t.add_column(f"Termin / nocy / wyż. ({name_b})", max_width=26, overflow="ellipsis")
    t.add_column(f"{name_a}", justify="right", width=8)
    t.add_column(f"{name_b}", justify="right", width=8)
    t.add_column("Różnica", justify="right", width=9)
    t.add_column("%", justify="right", width=7)
    t.add_column("Taniej u", width=10)

    for i, p in enumerate(pairings[:limit], 1):
        a, b = p.left, p.right
        cheaper = name_a if p.diff > 0 else (name_b if p.diff < 0 else "tyle samo")
        style = "green" if abs(p.diff_pct) >= 10 else ""
        flag = "" if p.match.same_region else " [yellow]~[/]"
        # Odległe terminy to już porównanie dwóch różnych wyjazdów — sygnalizujemy to.
        gap = abs((a.departure_date - b.departure_date).days) > 2
        term = (lambda o: f"[yellow]{o[0]}[/]" if gap else o[0])
        t.add_row(
            str(i), a.hotel_name + flag, a.region or b.region,
            f"{a.stars:.0f}" if a.stars else "-",
            term((f"{a.departure_date:%d.%m} / {a.nights}n / {a.board}",)),
            term((f"{b.departure_date:%d.%m} / {b.nights}n / {b.board}",)),
            f"{a.price:,}".replace(",", " "),
            f"{b.price:,}".replace(",", " "),
            f"[{style}]{abs(p.diff):,}[/]".replace(",", " ") if style
            else f"{abs(p.diff):,}".replace(",", " "),
            f"{abs(p.diff_pct):.1f}%",
            cheaper,
        )
    return t


def _only_here(offers: list[Offer], matched_ids: set[str], provider: str) -> list[Offer]:
    seen: dict[str, Offer] = {}
    for o in offers:
        hid = o.hotel_id or o.hotel_name
        if hid in matched_ids:
            continue
        cur = seen.get(hid)
        if cur is None or o.price < cur.price:
            seen[hid] = o
    return sorted(seen.values(), key=lambda o: o.price_ppn)


def cmd_compare(args) -> None:
    from ..cli import load_profile          # import leniwy — cli importuje cli_ext

    profile = load_profile(args.profile)
    console.print(f"[bold]{profile.name}[/] — {profile.country}, "
                  f"{profile.date_from}…{profile.date_to}, "
                  f"{profile.nights_min}-{profile.nights_max} nocy, {profile.adults} os.")
    console.print(f"Pobieram po {args.limit} ofert z każdego źródła…")

    wak, rpl = WakacjeProvider(delay=args.delay), RplProvider(delay=args.delay)
    offers_a = _fetch(wak, profile, args.limit)
    offers_b = _fetch(rpl, profile, args.limit)

    if not offers_a or not offers_b:
        missing = rpl.name if offers_a else wak.name
        console.print(f"\n[yellow]Brak ofert z {missing} — porównanie wymaga dwóch "
                      f"źródeł. Pokazuję tylko to, co się udało pobrać.[/]")
        single = offers_a or offers_b
        if single:
            best = sorted(single, key=lambda o: o.price_ppn)[:args.top]
            t = Table(title=f"Tylko {single[0].provider}", header_style="bold")
            for col in ("Hotel", "Region", "Termin", "N", "Wyż.", "Cena/os"):
                t.add_column(col)
            for o in best:
                t.add_row(o.hotel_name, o.region, f"{o.departure_date:%d.%m}",
                          str(o.nights), o.board, f"{o.price:,}".replace(",", " "))
            console.print(t)
        return

    matches = match_hotels(hotels_from_offers(offers_a), hotels_from_offers(offers_b))
    auto = [m for m in matches if m.status == "auto"]
    ambiguous = [m for m in matches if m.status == "ambiguous"]

    store = AliasStore(DB_PATH)       # tworzy i uzupełnia wyłącznie hotel_alias
    saved = store.save_matches(matches)
    store.close()

    by_a, by_b = _offers_by_hotel(offers_a), _offers_by_hotel(offers_b)
    pairings: list[Pairing] = []
    unpairable = 0
    for m in auto:
        pair = _best_pair(by_a.get(m.left.hotel_id, []), by_b.get(m.right.hotel_id, []))
        if pair is None:
            unpairable += 1           # ten sam hotel, ale nieporównywalne terminy
            continue
        pairings.append(Pairing(m, *pair))
    pairings.sort(key=lambda p: p.diff_pct, reverse=True)

    console.print()
    if pairings:
        console.print(_table(pairings, wak.name, rpl.name, args.top))
        console.print(f"[dim]„Różnica” liczona w zł za osobę: {rpl.name} minus "
                      f"{wak.name}. [yellow]~[/] = hotel dopasowany tylko po kraju "
                      f"(regiony nazwane inaczej po obu stronach); [yellow]żółty "
                      f"termin[/] = najbliższe porównywalne wyjazdy dzieli >2 dni.[/]")
    elif auto:
        console.print("[yellow]Żaden wspólny hotel nie ma porównywalnych ofert "
                      "(ta sama rodzina wyżywienia, różnica nocy ≤ 1).[/]")
    else:
        console.print(f"[yellow]Żaden hotel nie wystąpił w obu próbkach.[/]\n"
                      f"[dim]Oba źródła sortujemy od najtańszych, a ich tanie ogony "
                      f"bywają rozłączne — część wspólna rośnie z wielkością próbki. "
                      f"Spróbuj [bold]--limit {max(args.limit * 4, 150)}[/bold].[/]")
    if unpairable:
        console.print(f"[dim]{unpairable} wspólnych hoteli pominięto — oferty "
                      f"nieporównywalne (inne wyżywienie lub długość pobytu).[/]")

    matched_a = {m.left.hotel_id for m in auto}
    matched_b = {m.right.hotel_id for m in auto}
    only_a = _only_here(offers_a, matched_a, wak.name)
    only_b = _only_here(offers_b, matched_b, rpl.name)

    for name, only in ((wak.name, only_a), (rpl.name, only_b)):
        console.print(f"\n[bold]Tylko u {name}:[/] {len(only)} hoteli")
        for o in only[:args.only]:
            console.print(f"   [dim]{o.price:>5} zł/os · {o.price_ppn:>3.0f} zł/os/noc · "
                          f"{o.stars:.0f}★ · {o.region[:18]:18} · {o.hotel_name}[/]")
        if len(only) > args.only:
            console.print(f"   [dim]… i {len(only) - args.only} więcej[/]")

    console.print(f"\n[dim]Dopasowania hoteli: {len(auto)} pewnych, "
                  f"{len(ambiguous)} niepewnych; {saved} wierszy w hotel_alias "
                  f"({DB_PATH}). Oferty NIE zostały zapisane do bazy.[/]")
    if ambiguous:
        console.print("[dim]Niepewne (0.60–0.85 — do rozstrzygnięcia przez AI):[/]")
        for m in ambiguous[:args.only]:
            console.print(f"   [dim]{m.confidence:.2f}  "
                          f"{m.left.name}  ⟷  {m.right.name}[/]")


def register(sub) -> None:
    c = sub.add_parser("compare", help="porównaj ceny tych samych hoteli u dwóch dostawców")
    c.add_argument("profile")
    # Poniżej ~120 ofert części wspólnej zwykle nie ma: tanie ogony obu serwisów
    # są rozłączne, bo wakacje.pl agreguje kilkunastu organizatorów, a r.pl tylko Rainbow.
    c.add_argument("--limit", type=int, default=200, help="ile ofert pobrać z każdego źródła")
    c.add_argument("--top", type=int, default=25, help="ile wierszy pokazać w tabeli")
    c.add_argument("--only", type=int, default=8, help="ile pozycji w sekcjach „tylko u…”")
    c.add_argument("--delay", type=float, default=1.5, help="przerwa między zapytaniami [s]")
    c.set_defaults(func=cmd_compare)
