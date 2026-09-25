"""SQLite persistence. Mutating account operations use BEGIN IMMEDIATE."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

from .domain import ValidationError, canonical, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prices(
 id TEXT PRIMARY KEY, market TEXT NOT NULL, symbol TEXT NOT NULL,
 event_at TEXT NOT NULL, available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
 price TEXT NOT NULL, volume INTEGER NOT NULL, source TEXT NOT NULL, synthetic INTEGER NOT NULL,
 UNIQUE(market,symbol,event_at,available_at,source));
CREATE INDEX IF NOT EXISTS prices_pit ON prices(market,symbol,event_at,available_at);
CREATE TABLE IF NOT EXISTS news(
 id TEXT PRIMARY KEY, market TEXT NOT NULL, symbol TEXT NOT NULL,
 published_at TEXT NOT NULL, available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
 headline TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, synthetic INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(
 market TEXT PRIMARY KEY, currency TEXT NOT NULL, cash TEXT NOT NULL,
 initial_cash TEXT NOT NULL, clock TEXT, halted INTEGER NOT NULL DEFAULT 0,
 halt_reason TEXT NOT NULL DEFAULT '', policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions(
 market TEXT NOT NULL, symbol TEXT NOT NULL, quantity INTEGER NOT NULL,
 cost_basis TEXT NOT NULL, PRIMARY KEY(market,symbol));
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, run_key TEXT UNIQUE NOT NULL, market TEXT NOT NULL,
 as_of TEXT NOT NULL, created_at TEXT NOT NULL, mode TEXT NOT NULL,
 strategy TEXT NOT NULL, status TEXT NOT NULL, request_hash TEXT NOT NULL,
 snapshot_hash TEXT NOT NULL, snapshot TEXT NOT NULL, policy TEXT NOT NULL,
 decision TEXT, provider TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, intent_key TEXT UNIQUE NOT NULL, run_id TEXT NOT NULL,
 market TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
 quantity INTEGER NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
 limit_price TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id));
CREATE TABLE IF NOT EXISTS fills(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL, price_id TEXT NOT NULL,
 quantity INTEGER NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL, event_at TEXT NOT NULL,
 UNIQUE(order_id,price_id), FOREIGN KEY(order_id) REFERENCES orders(id));
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, config TEXT NOT NULL,
 data_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
 BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
 BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS pilot_settings_no_update BEFORE UPDATE ON settings WHEN OLD.key GLOB 'paper_pilot*'
 BEGIN SELECT RAISE(ABORT,'paper pilot records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS pilot_settings_no_delete BEFORE DELETE ON settings WHEN OLD.key GLOB 'paper_pilot*'
 BEGIN SELECT RAISE(ABORT,'paper pilot records are append-only'); END;
"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO settings VALUES('schema_version','1')")

    def connect(self):
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def audit(con, kind: str, payload: dict):
        con.execute("INSERT INTO audit(at,kind,payload) VALUES(?,?,?)", (now(), kind, canonical(payload)))

    def account(self, mkt: str, con=None) -> dict:
        if con is None:
            with self.connect() as db:
                return self.account(mkt, db)
        row = con.execute("SELECT * FROM accounts WHERE market=?", (mkt,)).fetchone()
        if row is None:
            raise ValidationError(f"Initialize the {mkt} paper account first")
        result = dict(row)
        result["policy"] = json.loads(result["policy"])
        return result

    def state(self) -> dict:
        with self.connect() as con:
            tables = {}
            for name in ("accounts", "positions", "orders", "fills"):
                tables[name] = [dict(row) for row in con.execute(f"SELECT * FROM {name}")]
            tables["runs"] = [dict(row) for row in con.execute(
                "SELECT id,run_key,market,as_of,mode,strategy,status,snapshot_hash,decision,provider,error "
                "FROM runs ORDER BY rowid DESC LIMIT 100")]
            tables["audit"] = [dict(row) for row in con.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT 30")]
            tables["experiments"] = [dict(row) for row in con.execute("SELECT * FROM experiments ORDER BY created_at DESC LIMIT 20")]
            tables["counts"] = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                                for t in ("prices", "news", "runs", "orders", "fills")}
            tables["synthetic_prices"] = con.execute("SELECT count(*) FROM prices WHERE synthetic=1").fetchone()[0]
            tables["paper_pilot"] = {row["key"]: json.loads(row["value"]) for row in con.execute(
                "SELECT key,value FROM settings WHERE key GLOB 'paper_pilot*' ORDER BY key")}
            tables["execution"] = "PAPER_ONLY"
            return tables
