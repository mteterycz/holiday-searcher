"""Podkomendy AI: `hs enrich`, `hs vibe`, `hs ai-usage`.

Wszystkie trzy mają działać BEZ klucza Gemini — wtedy pokazują to, co da się
pokazać bez modelu (opinie, werdykty z cache'u, zużycie limitów) i mówią wprost,
czego brakuje. Wysypanie się dlatego, że nie ma klucza, byłoby najgorszą
możliwą reakcją: faza 3 ma być dokładką do fazy 1, a nie warunkiem jej działania.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..ai.client import GeminiClient, GeminiError
from ..ai.opinions import HotelOpinions, WakacjeOpinions, slug_from_url
from ..ai.pool import ROLE_BULK_VERDICT, ROLE_DEEP, ModelPool
from ..ai.prompts import PROMPT_VERSION, VIBE_SCHEMA, VIBE_SYSTEM, build_vibe_user
from ..ai.verdicts import VerdictService, VerdictStore
from ..models import Offer
from ..paths import DB_PATH
from ..scoring import Scored, score_all

console = Console()


# --------------------------------------------------------------- oferty z bazy

def _offers_from_db(db, profile) -> list[Offer]:
    """Odtwarza `Offer` z bazy fazy 1. Cena pochodzi z NAJNOWSZEGO snapshotu —
    tabela `offer` jej nie trzyma, bo to ona się zmienia (patrz storage.py).
    `price_old` przepada, więc składnik 'promocja' w scoringu wychodzi 0 —
    do rankingu hoteli pod AI to bez znaczenia, do rankingu ofert już nie."""
    rows = db.execute("""
        SELECT o.*, (SELECT price FROM price_snapshot p
                     WHERE p.offer_key = o.key ORDER BY p.id DESC LIMIT 1) AS price
        FROM offer o
    """).fetchall()

    boards = {b.upper() for b in (profile.boards or [])}
    out: list[Offer] = []
    for r in rows:
        if not r["price"]:
            continue
        if profile.country and (r["country"] or "").lower() != profile.country.lower():
            continue
        dep = _as_date(r["departure_date"])
        if dep is None or not (profile.date_from <= dep <= profile.date_to):
            continue
        # API traktuje duration jako DNI (zależnie od operatora), więc pobyt
        # 7-dniowy bywa 6-nocny — tolerancja -1, spójnie z tym, co zwraca search.
        if not (profile.nights_min - 1 <= (r["nights"] or 0) <= profile.nights_max):
            continue
        if (r["stars"] or 0) < profile.stars_min:
            continue
        if boards and (r["board"] or "").upper() not in boards:
            continue
        if profile.rating_min and (r["rating"] or 0) < profile.rating_min:
            continue
        if profile.departures and (r["departure_code"] or "") not in profile.departures:
            continue
        # Oferty niewidziane od >48h najpewniej zniknęły z serwisu — nie rankingujemy
        # ich pod wzbogacanie (ich cena w bazie jest martwa).
        seen = _as_date(str(r["last_seen"])[:10])
        if seen is None or (date.today() - seen).days > 2:
            continue
        out.append(Offer(
            provider=r["provider"], hotel_name=r["hotel_name"], hotel_id=r["hotel_id"] or "",
            tour_operator=r["tour_operator"] or "", country=r["country"] or "",
            region=r["region"] or "", city=r["city"] or "", stars=float(r["stars"] or 0),
            departure_date=dep, return_date=_as_date(r["return_date"]) or dep,
            nights=int(r["nights"] or 0), board=r["board"] or "OTHER",
            board_raw=r["board_raw"] or "", departure_place=r["departure_place"] or "",
            departure_code=r["departure_code"] or "", room_type=r["room_type"] or "",
            price=int(r["price"]), price_old=0,
            rating=r["rating"], rating_count=r["rating_count"],
            url=r["url"] or "", raw_id="",
        ))
    return out


def _as_date(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _top_hotels(scored: list[Scored], n: int) -> list[Scored]:
    """Jeden wpis na hotel — najlepiej oceniona jego oferta. AI ocenia hotele,
    więc dziesięć terminów tego samego hotelu to wciąż jeden werdykt."""
    seen: set[str] = set()
    out: list[Scored] = []
    for s in scored:
        hid = s.offer.hotel_id
        if not hid or hid in seen:
            continue
        seen.add(hid)
        out.append(s)
        if len(out) >= n:
            break
    return out


def _load_candidates(args, profile, db) -> list[Scored]:
    offers = [] if getattr(args, "fresh", False) else _offers_from_db(db, profile)
    if not offers:
        from ..providers.wakacje import WakacjeProvider
        console.print("[dim]Baza nie ma ofert dla tego profilu — pobieram świeże…[/]")
        offers = WakacjeProvider().search(profile, limit=getattr(args, "limit", 120))
    if not offers:
        return []
    return score_all(offers)


def _fmt(v) -> str:
    return "–" if v is None else str(v)


# ------------------------------------------------------------------- hs enrich

def cmd_enrich(args):
    from ..cli import load_profile
    from ..storage import Storage

    profile = load_profile(args.profile)
    store = Storage(DB_PATH)
    scored = _load_candidates(args, profile, store.db)
    if not scored:
        console.print("[yellow]Brak ofert do wzbogacenia — najpierw `hs search`.[/]")
        return

    hotels = _top_hotels(scored, args.top)
    pool = ModelPool(store.db)
    vstore = VerdictStore(store.db)
    client = GeminiClient()
    fetcher = WakacjeOpinions(delay=args.delay)
    service = VerdictService(vstore, pool, client=client, fetcher=fetcher)
    model = pool.chain(ROLE_BULK_VERDICT)[0]

    console.print(f"[bold]{profile.name}[/] — top {len(hotels)} hoteli wg scoringu "
                  f"(prompt v{PROMPT_VERSION}, model {model})")

    rows: list[tuple[Scored, HotelOpinions | None, object]] = []
    problems: list[str] = []
    for s in hotels:
        o = s.offer
        cached = service.cached(o.hotel_id)
        ops: HotelOpinions | None = None
        verdict = cached
        if cached is None:
            ops = fetcher.fetch(o.hotel_id, slug=slug_from_url(o.url), url=o.url)
            if client.available:
                verdict = service.get_or_create(
                    o.hotel_id, o.hotel_name, o.region, url=o.url, opinions=ops,
                )
                if verdict is None and service.last_error:
                    problems.append(f"{o.hotel_name}: {service.last_error}")
        rows.append((s, ops, verdict))

    have_verdicts = any(v is not None for _, _, v in rows)
    if have_verdicts:
        console.print(_verdict_table(rows, profile.name))
    if not client.available:
        n = sum(1 for _, _, v in rows if v is None)
        console.print(_opinions_table([(s, ops) for s, ops, v in rows if v is None],
                                      profile.name))
        console.print(
            f"\n[yellow]Brak GEMINI_API_KEY — {n} z {len(rows)} hoteli czeka na ocenę AI.[/]\n"
            "[dim]Powyżej surowe opinie z wakacje.pl (to samo, co dostałby model).\n"
            "Klucz: wpisz GEMINI_API_KEY do config/.env — patrz docs/faza3-ai.md.[/]"
        )
    elif not have_verdicts:
        console.print("[yellow]Nie udało się wygenerować ani jednego werdyktu.[/]")

    for p in problems[:5]:
        console.print(f"[dim]· {p}[/]")


def _verdict_table(rows, profile_name: str) -> Table:
    t = Table(title=f"Ocena AI hoteli — {profile_name}", header_style="bold")
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Hotel", max_width=26, overflow="ellipsis")
    t.add_column("Jednym zdaniem", max_width=46, overflow="fold")
    t.add_column("Plaża", justify="center", width=5)
    t.add_column("Jedz.", justify="center", width=5)
    t.add_column("Czyst.", justify="center", width=6)
    t.add_column("Cisza", justify="center", width=5)
    t.add_column("Rodzin.", justify="center", width=7)
    t.add_column("Czerwone flagi", max_width=30, overflow="fold", style="red")
    i = 0
    for s, _ops, v in rows:
        if v is None:
            continue
        i += 1
        t.add_row(str(i), s.offer.hotel_name, v.one_liner or "–",
                  _fmt(v.beach), _fmt(v.food), _fmt(v.cleanliness),
                  _fmt(v.noise), _fmt(v.family_friendly),
                  ", ".join(v.red_flags) or "–")
    return t


def _opinions_table(rows, profile_name: str) -> Table:
    t = Table(title=f"Opinie z wakacje.pl — {profile_name}", header_style="bold")
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Hotel", max_width=24, overflow="ellipsis")
    t.add_column("Ocena", justify="right", width=5)
    t.add_column("Opinii", justify="right", width=6)
    t.add_column("Plusy (z opinii)", max_width=34, overflow="fold", style="green")
    t.add_column("Minusy (z opinii)", max_width=34, overflow="fold", style="red")
    for i, (s, ops) in enumerate(rows, 1):
        if ops is None:
            t.add_row(str(i), s.offer.hotel_name, "–", "–", "–", "–")
            continue
        plus = _joined(o.advantage for o in ops.opinions)
        minus = _joined(o.defect for o in ops.opinions)
        t.add_row(str(i), s.offer.hotel_name,
                  f"{ops.rating:.1f}" if ops.rating else "–",
                  str(len(ops)) if len(ops) else (ops.error or "0"),
                  plus or "–", minus or "–")
    return t


def _joined(values, limit: int = 2, width: int = 45) -> str:
    out = []
    for v in values:
        v = (v or "").strip(" -*•")
        if v:
            out.append(v if len(v) <= width else v[:width - 1] + "…")
        if len(out) >= limit:
            break
    return "; ".join(out)


# --------------------------------------------------------------------- hs vibe

def cmd_vibe(args):
    from ..cli import load_profile
    from ..storage import Storage

    profile = load_profile(args.profile)
    if not (profile.vibe or "").strip():
        console.print(
            f"[yellow]Profil [bold]{profile.name}[/] nie ma pola `vibe`.[/]\n"
            "[dim]Dopisz w config/profiles.yaml, np.\n"
            "  vibe: \"spokojny hotel przy szerokiej piaszczystej plaży, "
            "bez animacji do północy\"[/]"
        )
        return

    store = Storage(DB_PATH)
    scored = _load_candidates(args, profile, store.db)
    if not scored:
        console.print("[yellow]Brak ofert — najpierw `hs search`.[/]")
        return

    hotels = _top_hotels(scored, args.top)
    pool = ModelPool(store.db)
    vstore = VerdictStore(store.db)
    client = GeminiClient()
    service = VerdictService(vstore, pool, client=client,
                             fetcher=WakacjeOpinions(delay=args.delay))

    payload = []
    missing = []
    for s in hotels:
        o = s.offer
        v = service.cached(o.hotel_id)
        if v is None and client.available:
            v = service.get_or_create(o.hotel_id, o.hotel_name, o.region, url=o.url)
        if v is None:
            missing.append(o.hotel_name)
            continue
        payload.append({"hotel_id": o.hotel_id, "name": o.hotel_name,
                        "region": o.region, "verdict": v.data})

    if not client.available:
        console.print(
            "[yellow]Brak GEMINI_API_KEY — dopasowanie do vibe'u wymaga modelu.[/]\n"
            f"[dim]Shortlista: {len(hotels)} hoteli, werdykty w cache: {len(payload)}.\n"
            "Klucz: GEMINI_API_KEY w config/.env — patrz docs/faza3-ai.md.[/]"
        )
        return
    if not payload:
        console.print("[yellow]Żaden hotel z shortlisty nie ma werdyktu — "
                      "uruchom najpierw `hs enrich`.[/]")
        return

    # JEDNO wywołanie na całą shortlistę: model 'deep' ma RPD=20, więc pętla
    # po hotelach spaliłaby limit w jednym przebiegu.
    model = pool.acquire(ROLE_DEEP)
    if model is None:
        console.print("[yellow]Wyczerpany limit dzienny modeli roli 'deep'.[/]")
        return

    names = {h["hotel_id"]: h["name"] for h in payload}
    try:
        raw = client.generate(model, VIBE_SYSTEM, build_vibe_user(profile.vibe, payload),
                              VIBE_SCHEMA)
    except GeminiError as exc:
        console.print(f"[red]Gemini padł: {exc}[/]")
        return

    matches = sorted((raw.get("matches") or []),
                     key=lambda m: m.get("vibe_score") or 0, reverse=True)
    t = Table(title=f"Dopasowanie do vibe'u — {profile.name} (model {model})",
              header_style="bold")
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Hotel", max_width=28, overflow="ellipsis")
    t.add_column("Vibe", justify="right", width=5)
    t.add_column("Dlaczego", overflow="fold")
    for i, m in enumerate(matches, 1):
        hid = str(m.get("hotel_id"))
        t.add_row(str(i), names.get(hid, hid), _fmt(m.get("vibe_score")),
                  str(m.get("why") or "–"))
    console.print(f"[dim]vibe: {profile.vibe}[/]")
    console.print(t)
    if missing:
        console.print(f"[dim]Bez werdyktu (pominięte): {', '.join(missing[:5])}[/]")


# ----------------------------------------------------------------- hs ai-usage

def cmd_ai_usage(args):
    from ..storage import Storage

    pool = ModelPool(Storage(DB_PATH).db)
    usage = pool.usage(days=args.days)
    t = Table(title="Zużycie limitów Gemini (per model, per dzień)", header_style="bold")
    t.add_column("Dzień", width=10)
    t.add_column("Model", max_width=24)
    t.add_column("Rola", max_width=13)
    t.add_column("Requesty", justify="right", width=8)
    t.add_column("Limit RPD", justify="right", width=9)
    t.add_column("Zostało", justify="right", width=8)
    if not usage:
        console.print("[dim]Zero wywołań AI — tabela ai_usage jest pusta.[/]")
        return
    for u in usage:
        left = None if u["rpd"] is None else u["rpd"] - u["requests"]
        style = "red" if left is not None and left <= 0 else ""
        t.add_row(u["day"], u["model"], u["role"], str(u["requests"]),
                  _fmt(u["rpd"]),
                  f"[{style}]{left}[/]" if style else _fmt(left))
    console.print(t)
    console.print("[dim]Każdy model ma OSOBNY limit — to nie jest wspólna pula.[/]")


# -------------------------------------------------------------------- register

def register(sub) -> None:
    e = sub.add_parser("enrich", help="ocena AI hoteli z top N ofert profilu")
    e.add_argument("profile")
    e.add_argument("--top", type=int, default=10, help="ilu hoteli dotyczy ocena")
    e.add_argument("--limit", type=int, default=120, help="ile ofert pobrać, gdy baza pusta")
    e.add_argument("--fresh", action="store_true", help="pomiń bazę, pobierz świeże oferty")
    e.add_argument("--delay", type=float, default=1.5, help="przerwa między pobraniami opinii [s]")
    e.set_defaults(func=cmd_enrich)

    v = sub.add_parser("vibe", help="dopasowanie hoteli do pola `vibe` z profilu")
    v.add_argument("profile")
    v.add_argument("--top", type=int, default=10, help="rozmiar shortlisty")
    v.add_argument("--limit", type=int, default=120)
    v.add_argument("--fresh", action="store_true")
    v.add_argument("--delay", type=float, default=1.5)
    v.set_defaults(func=cmd_vibe)

    u = sub.add_parser("ai-usage", help="zużycie dziennych limitów per model")
    u.add_argument("--days", type=int, default=7)
    u.set_defaults(func=cmd_ai_usage)
