import json
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Literal

from kraken.models import Claim

_CLAIM_COLS = ("subject", "attribute", "value", "speaker", "room_id", "message_id",
               "timestamp", "confidence", "source", "path", "line")
_CLAIM_SQL = ", ".join(_CLAIM_COLS)
_CLAIM_DDL = """subject TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL,
    speaker TEXT NOT NULL, room_id TEXT NOT NULL, message_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL, confidence REAL NOT NULL, source TEXT NOT NULL,
    path TEXT, line INTEGER"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS claims (id INTEGER PRIMARY KEY AUTOINCREMENT, {_CLAIM_DDL});
CREATE INDEX IF NOT EXISTS claims_sa ON claims (subject, attribute);
CREATE TABLE IF NOT EXISTS aliases (raw TEXT PRIMARY KEY, canonical TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pairs (
    lo INTEGER NOT NULL, hi INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY (lo, hi));
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, {_CLAIM_DDL},
    decided_by TEXT NOT NULL, supersedes INTEGER);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, text TEXT NOT NULL,
    origin_room TEXT NOT NULL, state TEXT NOT NULL, storm_mode TEXT NOT NULL,
    storm_round INTEGER NOT NULL, brief TEXT, bids TEXT NOT NULL, winner TEXT,
    rooms TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_JSON_FIELDS = {"bids", "rooms", "data"}
_UPDATABLE = {"state", "storm_mode", "storm_round", "brief", "bids", "winner", "rooms", "data"}


@dataclass
class Request:
    id: int
    owner: str
    text: str
    origin_room: str
    state: str = "SPARK"
    storm_mode: str = "auto"
    storm_round: int = 0
    brief: str | None = None
    bids: list[dict] = field(default_factory=list)
    winner: str | None = None
    rooms: dict[str, str] = field(default_factory=dict)
    data: dict = field(default_factory=dict)


def _claim(row) -> Claim:
    return Claim(**dict(zip(_CLAIM_COLS, row)))


def _claim_values(c: Claim) -> tuple:
    return tuple(getattr(c, k) for k in _CLAIM_COLS)


class Ledger:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # claims
    def add_claim(self, c: Claim) -> int:
        marks = ", ".join("?" * len(_CLAIM_COLS))
        return self._exec(f"INSERT INTO claims ({_CLAIM_SQL}) VALUES ({marks})",
                          _claim_values(c)).lastrowid

    def get_claim(self, claim_id: int) -> Claim:
        rows = self._all(f"SELECT {_CLAIM_SQL} FROM claims WHERE id=?", (claim_id,))
        if not rows:
            raise KeyError(claim_id)
        return _claim(rows[0])

    def claims_for(self, subject: str, attribute: str) -> list[tuple[int, Claim]]:
        rows = self._all(f"SELECT id, {_CLAIM_SQL} FROM claims WHERE subject=? AND attribute=? "
                         "ORDER BY id", (subject, attribute))
        return [(r[0], _claim(r[1:])) for r in rows]

    # aliases
    def alias(self, raw: str) -> str | None:
        rows = self._all("SELECT canonical FROM aliases WHERE raw=?", (raw,))
        return rows[0][0] if rows else None

    def set_alias(self, raw: str, canonical: str) -> None:
        self._exec("INSERT INTO aliases (raw, canonical) VALUES (?, ?) "
                   "ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical", (raw, canonical))

    def known_subjects(self) -> list[str]:
        return [r[0] for r in self._all("SELECT DISTINCT subject FROM claims ORDER BY subject")]

    # pairs
    def pair_seen(self, id_a: int, id_b: int) -> bool:
        lo, hi = sorted((id_a, id_b))
        return bool(self._all("SELECT 1 FROM pairs WHERE lo=? AND hi=?", (lo, hi)))

    def mark_pair(self, id_a: int, id_b: int,
                  status: Literal["raised", "dismissed", "resolved"]) -> None:
        lo, hi = sorted((id_a, id_b))
        self._exec("INSERT INTO pairs (lo, hi, status) VALUES (?, ?, ?) "
                   "ON CONFLICT(lo, hi) DO UPDATE SET status=excluded.status", (lo, hi, status))

    def dismissal_count(self, subject: str, attribute: str) -> int:
        return self._all(
            "SELECT COUNT(*) FROM pairs p JOIN claims c ON c.id = p.lo "
            "WHERE p.status='dismissed' AND c.subject=? AND c.attribute=?",
            (subject, attribute))[0][0]

    # decisions
    def add_decision(self, c: Claim, decided_by: str, supersedes: int | None) -> int:
        marks = ", ".join("?" * (len(_CLAIM_COLS) + 2))
        return self._exec(
            f"INSERT INTO decisions ({_CLAIM_SQL}, decided_by, supersedes) VALUES ({marks})",
            _claim_values(c) + (decided_by, supersedes)).lastrowid

    def decisions(self) -> list[tuple[int, Claim, str]]:
        rows = self._all(
            f"SELECT id, {_CLAIM_SQL}, decided_by FROM decisions d "
            "WHERE d.id NOT IN (SELECT supersedes FROM decisions WHERE supersedes IS NOT NULL) "
            "ORDER BY id DESC")
        return [(r[0], _claim(r[1:-1]), r[-1]) for r in rows]

    # requests
    def new_request(self, owner: str, text: str, origin_room: str, state: str = "SPARK",
                    storm_mode: str = "auto") -> int:
        return self._exec(
            "INSERT INTO requests (owner, text, origin_room, state, storm_mode, storm_round, "
            "brief, bids, winner, rooms, data) VALUES (?, ?, ?, ?, ?, 0, NULL, '[]', NULL, "
            "'{}', '{}')", (owner, text, origin_room, state, storm_mode)).lastrowid

    def get_request(self, rid: int) -> Request:
        rows = self._all("SELECT id, owner, text, origin_room, state, storm_mode, storm_round, "
                         "brief, bids, winner, rooms, data FROM requests WHERE id=?", (rid,))
        if not rows:
            raise KeyError(rid)
        r = rows[0]
        return Request(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                       json.loads(r[8]), r[9], json.loads(r[10]), json.loads(r[11]))

    def open_requests(self) -> list[Request]:
        ids = [r[0] for r in self._all("SELECT id FROM requests WHERE state != 'CLOSED' ORDER BY id")]
        return [self.get_request(i) for i in ids]

    def update_request(self, rid: int, **fields) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        if not fields:
            return
        vals = [json.dumps(v) if k in _JSON_FIELDS else v for k, v in fields.items()]
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE requests SET {sets} WHERE id=?", (*vals, rid))

    def bind_room(self, rid: int, stage: str, room_id: str) -> None:
        with self._lock:
            rooms = self.get_request(rid).rooms
            rooms[stage] = room_id
            self.update_request(rid, rooms=rooms)

    def request_for_room(self, room_id: str) -> int | None:
        for rid, rooms in self._all("SELECT id, rooms FROM requests ORDER BY id"):
            if room_id in json.loads(rooms).values():
                return rid
        return None

    # meta
    def get_meta(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def last_sequence(self) -> int | None:
        v = self.get_meta("last_sequence")
        return None if v is None else int(v)

    def set_last_sequence(self, n: int) -> None:
        self.set_meta("last_sequence", str(n))
