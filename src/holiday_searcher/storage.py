"""SQLite. Kluczowa zasada: price_snapshot jest append-only — nigdy nie nadpisujemy
ceny. Z tego biorą się historia, wykrywanie obniżek i odporność na to,
że oferta na chwilę zniknie z wyników."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS offer (
    key             TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    hotel_name      TEXT NOT NULL,
    hotel_id        TEXT,
    tour_operator   TEXT,
    country         TEXT, region TEXT, city TEXT,
    stars           REAL,
    departure_date  TEXT NOT NULL,
    return_date     TEXT,
    nights          INTEGER NOT NULL,
    board           TEXT, board_raw TEXT,
    departure_place TEXT, departure_code TEXT,
    room_type       TEXT,
    rating          REAL, rating_count INTEGER,
    url             TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_snapshot (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key  TEXT NOT NULL REFERENCES offer(key),
    ts         TEXT NOT NULL,
    price      INTEGER NOT NULL,
    price_ppn  REAL NOT NULL,
    run_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_offer ON price_snapshot(offer_key, ts);

CREATE TABLE IF NOT EXISTS run (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    provider TEXT,
    found    INTEGER,
    note     TEXT
);
"""


class Storage:
    def __init__(self, path: str | Path = "data/offers.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def start_run(self, profile: str, provider: str) -> int:
        cur = self.db.execute(
            "INSERT INTO run(profile, ts, provider) VALUES (?,?,?)",
            (profile, datetime.now().isoformat(timespec="seconds"), provider),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, found: int, note: str = "") -> None:
        self.db.execute("UPDATE run SET found=?, note=? WHERE id=?", (found, note, run_id))
        self.db.commit()

    def save(self, offers: list[Offer], run_id: int | None = None) -> tuple[int, int]:
        """Zwraca (liczba nowych ofert, liczba zapisanych snapshotów)."""
        now = datetime.now().isoformat(timespec="seconds")
        new = 0
        for o in offers:
            exists = self.db.execute("SELECT 1 FROM offer WHERE key=?", (o.key,)).fetchone()
            if exists:
                self.db.execute("UPDATE offer SET last_seen=?, rating=?, rating_count=?, url=? WHERE key=?",
                                (now, o.rating, o.rating_count, o.url, o.key))
            else:
                new += 1
                self.db.execute(
                    """INSERT INTO offer(key,provider,hotel_name,hotel_id,tour_operator,
                       country,region,city,stars,departure_date,return_date,nights,
                       board,board_raw,departure_place,departure_code,room_type,
                       rating,rating_count,url,first_seen,last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (o.key, o.provider, o.hotel_name, o.hotel_id, o.tour_operator,
                     o.country, o.region, o.city, o.stars,
                     o.departure_date.isoformat(), o.return_date.isoformat(), o.nights,
                     o.board, o.board_raw, o.departure_place, o.departure_code,
                     o.room_type, o.rating, o.rating_count, o.url, now, now),
                )
            self.db.execute(
                "INSERT INTO price_snapshot(offer_key, ts, price, price_ppn, run_id) VALUES (?,?,?,?,?)",
                (o.key, now, o.price, round(o.price_ppn, 2), run_id),
            )
        self.db.commit()
        return new, len(offers)

    def latest_prices(self) -> dict[str, int]:
        rows = self.db.execute("""
            SELECT offer_key, price FROM price_snapshot
            WHERE id IN (SELECT MAX(id) FROM price_snapshot GROUP BY offer_key)
        """).fetchall()
        return {r["offer_key"]: r["price"] for r in rows}

    def price_history(self, offer_key: str) -> list[tuple[str, int]]:
        rows = self.db.execute(
            "SELECT ts, price FROM price_snapshot WHERE offer_key=? ORDER BY ts", (offer_key,)
        ).fetchall()
        return [(r["ts"], r["price"]) for r in rows]

    def stats(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {
            "offers": q("SELECT COUNT(*) FROM offer"),
            "snapshots": q("SELECT COUNT(*) FROM price_snapshot"),
            "runs": q("SELECT COUNT(*) FROM run"),
        }
