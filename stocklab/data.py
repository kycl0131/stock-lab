"""Immutable ingestion and explicit archived-vs-observed time semantics."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from .domain import ValidationError, decimal, digest, integer, market, now, symbol, timestamp
from .store import Store


def synthetic_flag(value) -> int:
    if value in (True, 1, "1", "true", "True"):
        return 1
    if value in (False, 0, "0", "false", "False"):
        return 0
    raise ValidationError("synthetic must explicitly be true or false")


def import_csv(store: Store, path: str | Path, kind: str) -> int:
    with open(path, encoding="utf-8-sig", newline="") as file:
        return ingest(store, list(csv.DictReader(file)), kind)


def ingest(store: Store, rows: list[dict], kind: str) -> int:
    if kind not in ("prices", "news"):
        raise ValidationError("Import kind must be prices or news")
    normalized = []
    for index, original in enumerate(rows, 1):
        try:
            item = {"market": market(original["market"]), "symbol": symbol(original["symbol"]),
                    "available_at": timestamp(original["available_at"]),
                    "source": str(original["source"]).strip(),
                    "synthetic": synthetic_flag(original["synthetic"])}
            if not item["source"] or len(item["source"]) > 200:
                raise ValidationError("source is required and must be <= 200 characters")
            if kind == "prices":
                item.update(event_at=timestamp(original["event_at"]),
                            price=str(decimal(original["price"])), volume=integer(original["volume"]))
                if decimal(item["price"]) <= 0 or item["event_at"] > item["available_at"]:
                    raise ValidationError("Price must be positive; available_at cannot precede event_at")
            else:
                item.update(published_at=timestamp(original["published_at"]),
                            headline=str(original["headline"]).strip(), body=str(original.get("body", "")))
                if not item["headline"] or len(item["headline"]) > 500 or len(item["body"]) > 12000:
                    raise ValidationError("News requires a headline <= 500 and a body <= 12000 characters")
                if item["published_at"] > item["available_at"]:
                    raise ValidationError("available_at cannot precede published_at")
            item["id"] = digest(item)
            item["ingested_at"] = now()
            normalized.append(item)
        except (KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"Row {index}: {exc}") from exc
    count = 0
    with store.transaction() as con:
        for item in normalized:
            if kind == "prices":
                previous = con.execute("SELECT source,synthetic FROM prices WHERE market=? AND symbol=? LIMIT 1",
                                       (item["market"], item["symbol"])).fetchone()
                if previous and (previous["source"] != item["source"] or previous["synthetic"] != item["synthetic"]):
                    raise ValidationError("One source and one provenance per symbol; use a separate database for other feeds")
            columns = list(item)
            cursor = con.execute(f"INSERT OR IGNORE INTO {kind} ({','.join(columns)}) "
                                 f"VALUES ({','.join('?' for _ in columns)})", [item[c] for c in columns])
            if cursor.rowcount == 0 and not con.execute(f"SELECT 1 FROM {kind} WHERE id=?", (item["id"],)).fetchone():
                raise ValidationError("Conflicting price observation: provide a later available_at for a revision")
            count += cursor.rowcount
        Store.audit(con, "DATA_IMPORTED", {"kind": kind, "inserted": count, "received": len(rows)})
    return count


def snapshot(store: Store, mkt: str, as_of: str, *, lookback=5, visibility="replay", con=None) -> dict:
    market(mkt)
    as_of = timestamp(as_of)
    lookback = integer(lookback, minimum=1)
    if visibility not in ("replay", "recorded"):
        raise ValidationError("visibility must be replay or recorded")
    if con is None:
        with store.connect() as db:
            return snapshot(store, mkt, as_of, lookback=lookback, visibility=visibility, con=db)
    ingestion_filter = "AND ingested_at<=?" if visibility == "recorded" else ""
    args = (mkt, as_of, as_of) + ((as_of,) if visibility == "recorded" else ())
    rows = con.execute(f"SELECT * FROM prices WHERE market=? AND event_at<=? AND available_at<=? "
                       f"{ingestion_filter} ORDER BY event_at,available_at,id", args).fetchall()
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], {})[row["event_at"]] = dict(row)
    instruments = []
    for ticker, observations in sorted(by_symbol.items()):
        history = sorted(observations.values(), key=lambda x: x["event_at"])[-(lookback + 1):]
        instruments.append({"symbol": ticker, "quote": history[-1], "history": history})
    news_after = (datetime.fromisoformat(as_of) - timedelta(days=30)).isoformat(timespec="microseconds")
    news_args = (mkt, as_of, as_of, news_after) + ((as_of,) if visibility == "recorded" else ())
    news_rows = [dict(row) for row in con.execute(
        f"SELECT * FROM news WHERE market=? AND published_at<=? AND available_at<=? AND published_at>=? "
        f"{ingestion_filter} ORDER BY published_at DESC,id LIMIT 50", news_args)]
    # Local ingestion timestamps do not change the fingerprint of archived replay data.
    if visibility == "replay":
        for instrument in instruments:
            for row in instrument["history"]:
                row.pop("ingested_at", None)
        for row in news_rows:
            row.pop("ingested_at", None)
    return {"market": mkt, "as_of": as_of, "visibility": visibility,
            "lookback": lookback, "instruments": instruments, "news": news_rows,
            "synthetic": any(i["quote"]["synthetic"] for i in instruments) or any(n["synthetic"] for n in news_rows),
            "limitations": ["Imported timestamps and historical universe coverage are not independently verified.",
                            "Corporate actions must be normalized upstream; execution requires unadjusted tradable prices."]}
