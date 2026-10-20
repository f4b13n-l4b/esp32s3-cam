"""Persistance SQLite : courses, passages détectés, saisies de dossards, participants.

Toutes les requêtes sont paramétrées (aucune concaténation de chaîne SQL).
Mode WAL : lectures de l'interface concurrentes des écritures de la caméra.
"""

from __future__ import annotations

import csv
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS race (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_us  INTEGER NOT NULL,
    start_us    INTEGER,
    closed      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS passage (
    id          INTEGER PRIMARY KEY,
    race_id     INTEGER NOT NULL REFERENCES race(id),
    ts_us       INTEGER NOT NULL,
    count       INTEGER NOT NULL CHECK (count BETWEEN 0 AND 50),
    source      TEXT NOT NULL CHECK (source IN ('camera', 'manuel')),
    photo       TEXT,
    voided      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS passage_race_ts ON passage(race_id, ts_us);
CREATE TABLE IF NOT EXISTS bib_entry (
    id          INTEGER PRIMARY KEY,
    race_id     INTEGER NOT NULL REFERENCES race(id),
    seq         REAL NOT NULL,
    bib         TEXT NOT NULL,
    entered_us  INTEGER NOT NULL,
    voided      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS bib_race_seq ON bib_entry(race_id, seq);
CREATE TABLE IF NOT EXISTS participant (
    bib         TEXT PRIMARY KEY,
    nom         TEXT,
    prenom      TEXT,
    categorie   TEXT,
    club        TEXT
);
"""


def now_us() -> int:
    return time.time_ns() // 1000


@dataclass
class Passage:
    id: int
    ts_us: int
    count: int
    source: str
    photo: str | None
    voided: bool


@dataclass
class BibEntry:
    id: int
    seq: float
    bib: str
    entered_us: int
    voided: bool


class Store:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.lastrowid if cur.lastrowid is not None else cur.rowcount

    # -- course ------------------------------------------------------------

    def create_race(self, name: str) -> int:
        with self._lock:
            self._x("UPDATE race SET closed = 1 WHERE closed = 0")
            return self._x("INSERT INTO race(name, created_us) VALUES (?, ?)", (name, now_us()))

    def current_race(self) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM race ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def set_start(self, race_id: int, start_us: int) -> None:
        self._x("UPDATE race SET start_us = ? WHERE id = ?", (start_us, race_id))

    def set_closed(self, race_id: int, closed: bool) -> None:
        self._x("UPDATE race SET closed = ? WHERE id = ?", (int(closed), race_id))

    # -- passages ----------------------------------------------------------

    def add_passage(self, race_id: int, ts_us: int, count: int, source: str, photo: str | None = None) -> int:
        return self._x(
            "INSERT INTO passage(race_id, ts_us, count, source, photo) VALUES (?, ?, ?, ?, ?)",
            (race_id, ts_us, count, source, photo),
        )

    def set_passage_photo(self, passage_id: int, photo: str) -> None:
        self._x("UPDATE passage SET photo = ? WHERE id = ?", (photo, passage_id))

    def update_passage(self, passage_id: int, count: int | None = None, voided: bool | None = None) -> None:
        if count is not None:
            self._x("UPDATE passage SET count = ? WHERE id = ?", (count, passage_id))
        if voided is not None:
            self._x("UPDATE passage SET voided = ? WHERE id = ?", (int(voided), passage_id))

    def get_passage(self, passage_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM passage WHERE id = ?", (passage_id,))
        return rows[0] if rows else None

    def passages(self, race_id: int) -> list[Passage]:
        return [
            Passage(r["id"], r["ts_us"], r["count"], r["source"], r["photo"], bool(r["voided"]))
            for r in self._q("SELECT * FROM passage WHERE race_id = ? ORDER BY ts_us, id", (race_id,))
        ]

    # -- dossards ----------------------------------------------------------

    def add_bib(self, race_id: int, bib: str, before_id: int | None = None) -> int:
        with self._lock:
            seq = self._seq_before(race_id, before_id)
            return self._x(
                "INSERT INTO bib_entry(race_id, seq, bib, entered_us) VALUES (?, ?, ?, ?)",
                (race_id, seq, bib, now_us()),
            )

    def move_bib(self, race_id: int, bib_id: int, before_id: int | None) -> None:
        with self._lock:
            seq = self._seq_before(race_id, before_id, exclude=bib_id)
            self._x("UPDATE bib_entry SET seq = ? WHERE id = ? AND race_id = ?", (seq, bib_id, race_id))

    def set_bib_voided(self, bib_id: int, voided: bool) -> None:
        self._x("UPDATE bib_entry SET voided = ? WHERE id = ?", (int(voided), bib_id))

    def bibs(self, race_id: int) -> list[BibEntry]:
        return [
            BibEntry(r["id"], r["seq"], r["bib"], r["entered_us"], bool(r["voided"]))
            for r in self._q("SELECT * FROM bib_entry WHERE race_id = ? ORDER BY seq, id", (race_id,))
        ]

    def _seq_before(self, race_id: int, before_id: int | None, exclude: int | None = None) -> float:
        """Numéro d'ordre pour insérer avant ``before_id`` (ou à la fin si None)."""
        rows = [
            r
            for r in self._q("SELECT id, seq FROM bib_entry WHERE race_id = ? ORDER BY seq, id", (race_id,))
            if r["id"] != exclude
        ]
        if before_id is None:
            return (rows[-1]["seq"] + 1.0) if rows else 1.0
        for i, r in enumerate(rows):
            if r["id"] == before_id:
                prev = rows[i - 1]["seq"] if i > 0 else r["seq"] - 1.0
                return (prev + r["seq"]) / 2.0
        raise KeyError(f"dossard {before_id} introuvable")

    # -- participants ------------------------------------------------------

    def load_participants_csv(self, path: str | Path) -> int:
        """Charge un CSV (séparateur ; ou ,) avec au moins une colonne « dossard »."""
        text = Path(path).read_text(encoding="utf-8-sig")
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        rows = []
        for r in reader:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            bib = r.get("dossard") or r.get("bib")
            if bib:
                rows.append((bib, r.get("nom"), r.get("prenom") or r.get("prénom"), r.get("categorie") or r.get("catégorie"), r.get("club")))
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM participant")
                self._db.executemany("INSERT OR REPLACE INTO participant VALUES (?, ?, ?, ?, ?)", rows)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return len(rows)

    def participants(self) -> dict[str, dict]:
        return {r["bib"]: dict(r) for r in self._q("SELECT * FROM participant")}
