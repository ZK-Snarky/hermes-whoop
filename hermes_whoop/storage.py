"""Private atomic files and deduplicated SQLite archive; no runtime state in source."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager, closing
from pathlib import Path

from .common import WhoopError, record_time

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def write_private(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.raw = self.root / "raw"
        self.raw.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.raw, 0o700)
        self.db = self.root / "archive.sqlite3"
        fd = os.open(self.db, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.db, 0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                  endpoint TEXT NOT NULL, record_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                  fetched_at TEXT NOT NULL, raw_json TEXT NOT NULL,
                  PRIMARY KEY(endpoint, record_id));
                CREATE INDEX IF NOT EXISTS record_time ON records(endpoint, timestamp DESC);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.db, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @contextmanager
    def lock(self, name="auth"):
        path = self.root / (name + ".lock")
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(str(path), threading.RLock())
        with lock:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "a+b") as stream:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    stream.write(b"\0")
                    stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream, fcntl.LOCK_UN)

    def read_json(self, name):
        path = self.root / name
        if not path.exists():
            return {}
        os.chmod(path, 0o600)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            raise WhoopError("invalid_local_state", "restore_private_state") from None

    def write_json(self, name, value):
        write_private(self.root / name, value)

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def save(self, kind, records, fetched_at, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        target = self.raw / (kind + "-" + digest + ".json")
        if not target.exists():
            write_private(target, payload)
        rows = []
        for record in records:
            if not isinstance(record, dict):
                raise WhoopError("invalid_provider_response", "retry")
            raw = json.dumps(record, sort_keys=True, allow_nan=False)
            rid = record.get("cycle_id") if kind == "recovery" else record.get("id")
            if kind == "profile":
                rid = "current"
            elif kind == "body":
                rid = hashlib.sha256(raw.encode()).hexdigest()[:24]
            if rid is None:
                raise WhoopError("missing_record_id", "retry")
            timestamp = record_time(kind, record, fetched_at)
            rows.append((kind, str(rid), timestamp, fetched_at, raw))
        with self.connect() as db:
            db.executemany("""INSERT INTO records VALUES (?,?,?,?,?)
                ON CONFLICT(endpoint,record_id) DO UPDATE SET timestamp=excluded.timestamp,
                fetched_at=excluded.fetched_at,raw_json=excluded.raw_json""", rows)

    def rows(self, kind, *, record_id=None, start=None, end=None, limit=100, offset=0):
        where, args = ["endpoint=?"], [kind]
        for clause, value in (("record_id=?", record_id), ("timestamp>=?", start), ("timestamp<?", end)):
            if value is not None:
                where.append(clause)
                args.append(value)
        args += [limit, offset]
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM records WHERE " + " AND ".join(where) + " ORDER BY timestamp DESC,record_id DESC LIMIT ? OFFSET ?", args)]

    def inventory(self):
        with self.connect() as db:
            return {row["endpoint"]: {"count": row["count"], "oldest": row["oldest"], "newest": row["newest"], "fetched_at": row["fetched_at"]} for row in db.execute("SELECT endpoint,COUNT(*) count,MIN(timestamp) oldest,MAX(timestamp) newest,MAX(fetched_at) fetched_at FROM records GROUP BY endpoint")}
