"""Podkomendy `hs web` (lokalny dashboard) i `hs export` (statyczna migawka).

Obie żyją w tym samym pliku, bo korzystają z tego samego kodu renderującego
(`holiday_searcher.web`) i tego samego trybu read-only na bazie.
Patrz docs/faza5-dashboard.md.
"""
from __future__ import annotations

import webbrowser
from pathlib import Path

from ..paths import DB_PATH, ROOT
from ..web.server import DEFAULT_PORT, build_server
from ..web.static_export import DEFAULT_OUT, export_site


def cmd_web(args) -> None:
    server = build_server(port=args.port)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"Dashboard: {url}  (Ctrl+C, aby zakończyć)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymywanie…")
    finally:
        server.server_close()


def cmd_export(args) -> None:
    out = Path(args.out) if args.out else (ROOT / DEFAULT_OUT)
    db = Path(args.db) if getattr(args, "db", None) else DB_PATH
    files = export_site(db, out)
    print(f"Wyeksportowano {len(files)} plików do {out}")
    for f in files[:5]:
        print(f"  {f.relative_to(out)}")
    if len(files) > 5:
        print(f"  … i {len(files) - 5} więcej (strony ofert)")
    print(f"Otwórz: {out / 'index.html'}")


def register(sub) -> None:
    p = sub.add_parser("web", help="uruchom lokalny dashboard webowy (odczyt bazy)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (domyślnie {DEFAULT_PORT})")
    p.add_argument("--open", action="store_true", help="otwórz dashboard w przeglądarce")
    p.set_defaults(func=cmd_web)

    e = sub.add_parser("export", help="zapisz statyczną migawkę dashboardu (HTML bez serwera)")
    e.add_argument("--out", default=None, help=f"katalog docelowy (domyślnie {DEFAULT_OUT}/)")
    e.add_argument("--db", default=None, help="ścieżka do bazy (domyślnie data/offers.db)")
    e.set_defaults(func=cmd_export)
