"""Serwer HTTP dashboardu — http.server ze standardowej biblioteki, tylko odczyt.

Baza bywa równolegle zapisywana przez monitor (faza 2) i przez `hs search`,
więc każde żądanie otwiera własne połączenie w trybie read-only (URI
``mode=ro``) i ponawia próbę przy komunikacie "database is locked" zamiast
wywalać się na starcie."""
from __future__ import annotations

import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..paths import DB_PATH
from . import pages

DEFAULT_PORT = 8787
_LOCK_RETRIES = 5
_LOCK_DELAY_S = 0.2


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Łączy się z bazą tylko do odczytu; ponawia przy zablokowanej bazie."""
    uri = f"file:{db_path}?mode=ro"
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(_LOCK_RETRIES):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            last_err = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(_LOCK_DELAY_S * (attempt + 1))
    assert last_err is not None
    raise last_err


_connect_readonly = connect_readonly  # zgodność wsteczna


def _first(query: dict, name: str) -> str | None:
    vals = query.get(name)
    if not vals or not vals[0]:
        return None
    return vals[0]


def _first_int(query: dict, name: str) -> int | None:
    raw = _first(query, name)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _first_float(query: dict, name: str) -> float | None:
    raw = _first(query, name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class DashboardHandler(BaseHTTPRequestHandler):
    """db_path jest atrybutem klasy — build_server() tworzy podklasę na żądany plik,
    żeby wiele instancji serwera (np. w testach) mogło wskazywać różne bazy."""

    db_path: Path = DB_PATH
    server_version = "holiday-searcher-dashboard/2.0"

    def log_message(self, fmt: str, *args) -> None:  # ciszej niż domyślny stderr-spam
        pass

    def _send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            conn = connect_readonly(self.db_path)
        except sqlite3.OperationalError as exc:
            self._send_html(pages.render_error(f"Baza danych niedostępna: {exc}"), 503)
            return

        try:
            if path == "/":
                self._send_html(pages.render_index(conn, pages.ctx_for(conn, "index")))
            elif path == "/offers":
                self._send_html(pages.render_offers(
                    conn,
                    sort=_first(query, "sort") or "price",
                    country=_first(query, "country"),
                    max_price=_first_int(query, "max_price"),
                    min_rating=_first_float(query, "min_rating"),
                    ctx=pages.ctx_for(conn, "offers"),
                ))
            elif path == "/hotels":
                self._send_html(pages.render_hotels(
                    conn, _first(query, "sort") or "price", pages.ctx_for(conn, "hotels")))
            elif path == "/drops":
                self._send_html(pages.render_drops(conn, pages.ctx_for(conn, "drops")))
            elif path in ("/kalendarz", "/calendar"):
                self._send_html(pages.render_calendar(conn, pages.ctx_for(conn, "calendar")))
            elif path.startswith("/offer/"):
                key = unquote(path[len("/offer/"):]).rstrip("/")
                if not key:
                    self._send_html(pages.render_error("Brak klucza oferty w adresie"), 404)
                    return
                ctx = pages.ctx_for(conn, "offers")
                html = pages.render_offer_detail(conn, key, ctx)
                if html is None:
                    self._send_html(pages.render_error(f"Nie znaleziono oferty: {key}", ctx), 404)
                else:
                    self._send_html(html)
            else:
                self._send_html(pages.render_error("Nie znaleziono strony"), 404)
        except sqlite3.OperationalError as exc:
            self._send_html(pages.render_error(f"Błąd bazy danych: {exc}"), 503)
        finally:
            conn.close()


def build_server(port: int = DEFAULT_PORT, db_path: Path | str | None = None) -> ThreadingHTTPServer:
    """Buduje serwer bez uruchamiania — używane też przez testy (port=0 -> efemeryczny)."""
    resolved = Path(db_path) if db_path else DB_PATH
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"db_path": resolved})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def run(port: int = DEFAULT_PORT, db_path: Path | str | None = None) -> None:
    """Uruchamia serwer w bieżącym wątku (foreground) aż do Ctrl+C."""
    server = build_server(port, db_path)
    host, bound_port = server.server_address[:2]
    print(f"Dashboard: http://{host}:{bound_port}/  (Ctrl+C, aby zakończyć)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymywanie…")
    finally:
        server.server_close()
