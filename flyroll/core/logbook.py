"""SQLite logbook - stores recorded flights (like Sky Dolly's logbook).

Every channel is stored as one zlib-compressed numpy blob, which keeps a
two-hour airliner flight at a few megabytes.  A single flight can be exported
to / imported from a standalone ``.flyroll`` file (same schema, one flight)
or exported as CSV.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import zlib
from pathlib import Path
from typing import List, Optional

import numpy as np

from .channels import Channel
from .flight import Flight

SCHEMA = """
CREATE TABLE IF NOT EXISTS flight (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created   TEXT NOT NULL,
    title     TEXT,
    aircraft  TEXT,
    profile   TEXT,
    duration  REAL,
    samples   INTEGER,
    meta      TEXT
);
CREATE TABLE IF NOT EXISTS channel (
    flight_id INTEGER NOT NULL REFERENCES flight(id) ON DELETE CASCADE,
    key       TEXT NOT NULL,
    def       TEXT NOT NULL,
    dtype     TEXT NOT NULL,
    data      BLOB NOT NULL,
    PRIMARY KEY (flight_id, key)
);
"""


def _pack(arr: np.ndarray) -> bytes:
    return zlib.compress(np.ascontiguousarray(arr).tobytes(), 6)


def _unpack(blob: bytes, dtype: str) -> np.ndarray:
    return np.frombuffer(zlib.decompress(blob), dtype=np.dtype(dtype)).copy()


def _chan_to_json(c: Channel) -> str:
    return json.dumps({"key": c.key, "simvar": c.simvar, "unit": c.unit, "interp": c.interp,
                       "group": c.group, "label": c.label, "precise": c.precise, "settable": c.settable})


def _chan_from_json(s: str) -> Channel:
    return Channel(**json.loads(s))


class Logbook:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- write --------------------------------------------------------------
    def save(self, flight: Flight) -> int:
        m = flight.meta
        cur = self.db.execute(
            "INSERT INTO flight(created, title, aircraft, profile, duration, samples, meta) VALUES (?,?,?,?,?,?,?)",
            (m.get("created", ""), m.get("title", ""), m.get("aircraft", ""), m.get("profile_id", "default"),
             flight.duration, flight.samples, json.dumps(m)))
        fid = int(cur.lastrowid)
        rows = [(fid, "_t", json.dumps({"key": "_t"}), "float64", _pack(flight.t.astype(np.float64)))]
        for c in flight.channels:
            col = flight.columns.get(c.key)
            if col is None:
                continue
            rows.append((fid, c.key, _chan_to_json(c), str(col.dtype), _pack(col)))
        self.db.executemany("INSERT INTO channel(flight_id, key, def, dtype, data) VALUES (?,?,?,?,?)", rows)
        self.db.commit()
        flight.id = fid
        return fid

    def rename(self, flight_id: int, title: str) -> None:
        row = self.db.execute("SELECT meta FROM flight WHERE id=?", (flight_id,)).fetchone()
        meta = json.loads(row[0]) if row and row[0] else {}
        meta["title"] = title
        self.db.execute("UPDATE flight SET title=?, meta=? WHERE id=?", (title, json.dumps(meta), flight_id))
        self.db.commit()

    def delete(self, flight_id: int) -> None:
        self.db.execute("DELETE FROM channel WHERE flight_id=?", (flight_id,))
        self.db.execute("DELETE FROM flight WHERE id=?", (flight_id,))
        self.db.commit()

    # -- read ---------------------------------------------------------------
    def list(self) -> List[dict]:
        cur = self.db.execute(
            "SELECT id, created, title, aircraft, profile, duration, samples FROM flight ORDER BY id DESC")
        keys = ["id", "created", "title", "aircraft", "profile", "duration", "samples"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def load(self, flight_id: int) -> Flight:
        row = self.db.execute("SELECT meta FROM flight WHERE id=?", (flight_id,)).fetchone()
        if row is None:
            raise KeyError(f"flight {flight_id} not found")
        meta = json.loads(row[0] or "{}")
        channels: List[Channel] = []
        columns = {}
        t = np.zeros(0)
        for key, d, dtype, blob in self.db.execute(
                "SELECT key, def, dtype, data FROM channel WHERE flight_id=? ORDER BY rowid", (flight_id,)):
            arr = _unpack(blob, dtype)
            if key == "_t":
                t = arr
                continue
            channels.append(_chan_from_json(d))
            columns[key] = arr
        return Flight(channels, t, columns, meta, flight_id)

    # -- files --------------------------------------------------------------
    def export_file(self, flight_id: int, path: Path) -> None:
        path = Path(path)
        if path.exists():
            path.unlink()
        other = Logbook(path)
        try:
            other.save(self.load(flight_id))
        finally:
            other.close()

    def import_file(self, path: Path) -> List[int]:
        other = Logbook(Path(path))
        try:
            return [self.save(other.load(info["id"])) for info in reversed(other.list())]
        finally:
            other.close()

    @staticmethod
    def export_csv(flight: Flight, path: Path) -> None:
        keys = [c.key for c in flight.channels if c.key in flight.columns]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time_s"] + keys)
            cols = [flight.columns[k] for k in keys]
            for i in range(flight.samples):
                w.writerow([f"{flight.t[i]:.3f}"] + [f"{c[i]:.6g}" for c in cols])
