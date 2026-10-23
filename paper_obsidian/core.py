from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def today():
    return datetime.now(TZ).date().isoformat()


def digest(value):
    if not isinstance(value, (bytes, str)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def local_path(value, base=ROOT):
    value = str(value)
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", value):
        value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def load_config(path=None):
    p = local_path(path or "config.toml")
    c = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    for k in ("vault", "data", "whitelist"):
        c["paths"][k] = local_path(c["paths"][k])
    sub = Path(c["paths"].get("subfolder", ""))
    if sub.is_absolute() or ".." in sub.parts:
        raise ValueError("Vault 子目录必须是安全相对路径")
    c["paths"]["vault"] = c["paths"]["vault"] / sub
    return c


def secrets():
    values = dict(dotenv_values(ROOT / ".env"))
    keys = ("API_BASE_URL", "API_KEY", "MODEL", "OPENALEX_API_KEY",
            "TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL")
    return {k: os.environ.get(k, values.get(k) or "") for k in keys}


def safe_error(exc):
    # Never log arbitrary exception bodies: vendors may echo credentials or request text.
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" HTTP {code}" if code else "")


def atomic_write(path, content, backup_dir=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if path.exists():
        old = path.read_bytes()
        if old == raw:
            return False
        if backup_dir:
            bp = Path(backup_dir) / (digest(str(path))[:16] + "-" + digest(old)[:16] + path.suffix)
            bp.parent.mkdir(parents=True, exist_ok=True)
            if not bp.exists():
                bp.write_bytes(old)
    fd, tmp = tempfile.mkstemp(prefix=".paper-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


_held_locks = contextvars.ContextVar('paper_locks', default=frozenset())


@contextlib.contextmanager
def run_lock(data, *, maintenance=False, reentrant=False):
    """OS releases the advisory lock after crashes; stale PID files cannot block runs."""
    key = str(data.resolve())
    if key in _held_locks.get():
        if not reentrant:
            raise RuntimeError('另一次运行正在进行，请等待或使用 stop')
        yield
        return
    data.mkdir(parents=True, exist_ok=True)
    if (data / 'MAINTENANCE').exists() and not maintenance:
        raise RuntimeError('目录迁移维护中，禁止新运行；完成验证后恢复')
    p = data / "run.lock"
    with p.open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("另一次运行正在进行，请等待或使用 stop") from None
        if (data / 'MAINTENANCE').exists() and not maintenance:
            raise RuntimeError('目录迁移维护中，禁止新运行')
        if (data / 'running.json').exists():
            previous=(data / 'running.json').read_bytes()
            atomic_write(data / 'interrupted-markers' / (digest(previous)+'.json'), previous)
        atomic_write(data / "running.json", json.dumps({"pid": os.getpid(), "started": now()}))
        token = _held_locks.set(_held_locks.get() | {key})
        try:
            yield
        finally:
            _held_locks.reset(token)
            (data / "running.json").unlink(missing_ok=True)
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def doi_id(value):
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value or "", flags=re.I).strip().lower()


def arxiv_id(value):
    v = re.sub(r"^.*arxiv\.org/(abs|pdf)/", "", value or "").removesuffix(".pdf")
    return re.sub(r"v\d+$", "", v).strip()


class Store:
    def __init__(self, data):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data / "papers.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS papers(id TEXT PRIMARY KEY, body TEXT NOT NULL,
          created TEXT NOT NULL, updated TEXT NOT NULL, merged_into TEXT);
        CREATE TABLE IF NOT EXISTS identifiers(alias TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id));
        CREATE TABLE IF NOT EXISTS provenance(paper_id TEXT, source TEXT, hash TEXT, body TEXT, seen TEXT,
          PRIMARY KEY(paper_id,source,hash));
        CREATE TABLE IF NOT EXISTS stages(paper_id TEXT, stage TEXT, version TEXT, status TEXT, detail TEXT,
          updated TEXT, PRIMARY KEY(paper_id,stage));
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS usage(day TEXT, kind TEXT, n INTEGER, PRIMARY KEY(day,kind));
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, ended TEXT, report TEXT);
        CREATE TABLE IF NOT EXISTS admission(day TEXT, paper_id TEXT UNIQUE, PRIMARY KEY(day,paper_id));
        """)

    def get_state(self, key, default=None):
        r = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_state(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def used(self, kind):
        r = self.db.execute("SELECT n FROM usage WHERE day=? AND kind=?", (today(), kind)).fetchone()
        return r[0] if r else 0

    def spend(self, kind, limit):
        with self.db:
            if self.used(kind) >= limit:
                return False
            self.db.execute("INSERT INTO usage VALUES (?,?,1) ON CONFLICT(day,kind) DO UPDATE SET n=n+1", (today(), kind))
        return True

    def ingest(self, p, cap=50):
        aliases = sorted(set(p.get("aliases", [])))
        if not aliases:
            raise ValueError("缺少稳定标识，不按标题冒险合并")
        ids = sorted({r[0] for a in aliases for r in self.db.execute("SELECT paper_id FROM identifiers WHERE alias=?", (a,))})
        relations = []
        if not ids and p.get('source') != 'cnki':
            from .cnki import match
            matched, relations = match(p, self, cnki_only=True)
            if matched:
                ids = [matched]
                aliases = sorted(set(aliases + self.get(matched).get('aliases', [])))
                p = dict(p, aliases=aliases)
        if len(ids)>1 and any('cnki' in self.get(pid).get('sources', []) for pid in ids):
            raise ValueError('涉及知网的强标识桥接冲突，待确认；禁止自动合并')
        if not ids and self.used("candidates") >= cap:
            return None
        pid = ids[0] if ids else digest(aliases[0])[:20]
        with self.db:
            if not ids:
                self.spend("candidates", cap)
                self.db.execute("INSERT INTO papers VALUES (?,?,?,?,NULL)", (pid, "{}", now(), now()))
                self.db.execute("INSERT INTO admission VALUES (?,?)", (today(), pid))
            old = self.get(pid)
            # A bridge is allowed only by exact external aliases (DOI/arXiv/source IDs), never title similarity.
            for other in ids[1:]:
                extra = self.get(other)
                old = merge_metadata(old, extra)
                self.db.execute("UPDATE identifiers SET paper_id=? WHERE paper_id=?", (pid, other))
                self.db.execute("UPDATE papers SET merged_into=? WHERE id=?", (pid, other))
            merged = merge_metadata(old, p)
            merged["id"] = pid
            encoded = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            if encoded != json.dumps(old, ensure_ascii=False, sort_keys=True):
                self.db.execute("UPDATE papers SET body=?,updated=? WHERE id=?", (encoded, now(), pid))
            for a in aliases:
                self.db.execute("INSERT OR REPLACE INTO identifiers VALUES (?,?)", (a, pid))
            self.db.execute("INSERT OR IGNORE INTO provenance VALUES (?,?,?,?,?)",
                            (pid, p["source"], digest(p), json.dumps(p, ensure_ascii=False), now()))
        if relations:
            self.set_state('cnki:relations:'+pid, {'ids':relations,'status':'待确认，不自动合并'})
        return pid

    def get(self, pid):
        r = self.db.execute("SELECT body FROM papers WHERE id=?", (pid,)).fetchone()
        return json.loads(r[0]) if r else {}

    def papers(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM papers WHERE merged_into IS NULL ORDER BY created DESC,id")]

    def stage(self, pid, stage):
        r = self.db.execute("SELECT * FROM stages WHERE paper_id=? AND stage=?", (pid, stage)).fetchone()
        return dict(r) if r else None

    def record(self, pid, stage, version, status, detail):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO stages VALUES (?,?,?,?,?,?)",
                            (pid, stage, version, status, json.dumps(detail, ensure_ascii=False), now()))


def merge_metadata(old, new):
    p = dict(old)
    if new.get('source') == 'cnki' and old:
        # Local exports are source claims, never authority to overwrite verified data.
        for k, v in new.items():
            if k in ('aliases', 'sources', 'source_urls', 'cnki_ids'):
                if v: p[k] = sorted(set(p.get(k, []) + v))
            elif k == 'dates':
                if v: p[k] = dict(v, **p.get(k, {}))
            elif not p.get(k) and v not in (None, '', [], {}):
                p[k] = v
        p['sources'] = sorted(set(p.get('sources', []) + ['cnki']))
        return p
    established = old.get("publication", {}).get("status") in ("accepted", "published")
    incoming_preprint = new.get("publication", {}).get("status") in ("preprint", "submitted", "unknown")
    for k, v in new.items():
        if established and incoming_preprint and k in ("venue", "venue_kind", "issns", "date"):
            continue
        if k in ("id", "source") and p.get(k):
            continue
        if k in ("aliases", "issns", "sources"):
            p[k] = sorted(set(p.get(k, []) + v))
        elif k == "dates":
            p[k] = dict(p.get(k, {}), **{a: b for a, b in v.items() if b})
        elif v not in (None, "", [], {}):
            # Preprints cannot erase official publication evidence from another source.
            if k == "publication" and p.get(k, {}).get("status") in ("accepted", "published") and v.get("status") in ("preprint", "submitted", "unknown"):
                continue
            p[k] = v
    p["sources"] = sorted(set(p.get("sources", []) + [new.get("source", "unknown")]))
    return p
