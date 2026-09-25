"""Small, fixed-budget baseline comparisons, with the full trial databases retained."""
from __future__ import annotations

from pathlib import Path
from decimal import Decimal
import json
import uuid

from .data import ingest, snapshot
from .domain import RiskPolicy, ValidationError, canonical, decimal, digest, market, now
from .engine import advance, cancel, initialize, run
from .store import Store


def compare(store: Store, mkt: str, output: str | Path, *, lookback=5, top_k=3, weight="0.20") -> dict:
    market(mkt)
    account = store.account(mkt)
    with store.connect() as con:
        prices = [dict(r) for r in con.execute("SELECT * FROM prices WHERE market=? ORDER BY event_at,available_at,id", (mkt,))]
        news = [dict(r) for r in con.execute("SELECT * FROM news WHERE market=? ORDER BY published_at,id", (mkt,))]
    times = sorted({p["available_at"] for p in prices})
    if len(times) < lookback + 3:
        raise ValidationError("Not enough observations for a comparison")
    trial_id = uuid.uuid4().hex
    folder = Path(output).resolve() / trial_id
    folder.mkdir(parents=True)
    config = {"market": mkt, "strategies": ["equal", "momentum"], "lookback": lookback,
              "top_k": top_k, "weight": weight, "initial_cash": account["initial_cash"], "policy": account["policy"],
              "visibility": "replay", "rebalance": "each_observation_time"}
    fingerprint_rows = [{k: v for k, v in row.items() if k != "ingested_at"} for row in prices + news]
    fingerprint = digest(fingerprint_rows)
    with store.transaction() as con:
        con.execute("INSERT INTO experiments(id,created_at,config,data_hash,status) VALUES(?,?,?,?,?)",
                    (trial_id, now(), canonical(config), fingerprint, "RUNNING"))
        Store.audit(con, "EXPERIMENT_STARTED", {"id": trial_id, "config": config, "data_hash": fingerprint})
    try:
        results = []
        for method in config["strategies"]:
            trial = Store(folder / f"{method}.db")
            ingest(trial, prices, "prices")
            ingest(trial, news, "news")
            initialize(trial, mkt, account["initial_cash"], RiskPolicy(**account["policy"]))
            curve = []
            for index, at in enumerate(times):
                advance(trial, mkt, at)
                if index >= lookback and index < len(times) - 1:
                    with trial.connect() as con:
                        active = [row[0] for row in con.execute("SELECT id FROM orders WHERE status IN ('OPEN','PARTIAL')")]
                    for order_id in active:
                        cancel(trial, order_id)
                    run(trial, run_key=f"{method}:{at}", mkt=mkt, as_of=at, mode="paper", strategy=method,
                        lookback=lookback, top_k=top_k, weight=weight)
                state = trial.state()
                marks = {i["symbol"]: decimal(i["quote"]["price"]) for i in snapshot(trial, mkt, at)["instruments"]}
                equity = decimal(state["accounts"][0]["cash"]) + sum(p["quantity"] * marks[p["symbol"]] for p in state["positions"])
                curve.append({"at": at, "equity": str(equity)})
            initial = decimal(account["initial_cash"])
            peak, drawdown = initial, Decimal(0)
            for point in curve:
                equity = decimal(point["equity"])
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1)
            state = trial.state()
            results.append({"strategy": method, "return_pct": str((decimal(curve[-1]["equity"]) / initial - 1) * 100),
                            "max_drawdown_pct": str(drawdown * 100), "fill_count": len(state["fills"]),
                            "fees": str(sum(decimal(f["fee"]) for f in state["fills"])),
                            "turnover_notional": str(sum(decimal(f["price"]) * f["quantity"] for f in state["fills"])),
                            "rejected_orders": sum(o["status"] == "REJECTED" for o in state["orders"]),
                            "curve": curve, "database": str(trial.path)})
        result = {"id": trial_id, "market": mkt, "currency": account["currency"], "data_hash": fingerprint,
                  "synthetic": any(p["synthetic"] for p in prices), "results": results,
                  "limitations": ["No statistical alpha certification or inference cost estimate.",
                                  "Equal is a fixed-universe rebalancing reference, not an index buy-and-hold.",
                                  "Sampled-price limit fills omit queue priority, auctions, taxes, FX and market impact.",
                                  "No survivorship-bias or corporate-action certification; validate upstream data."]}
        (folder / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with store.transaction() as con:
            con.execute("UPDATE experiments SET status='COMPLETED',result=? WHERE id=?", (canonical(result), trial_id))
            Store.audit(con, "EXPERIMENT_COMPLETED", {"id": trial_id})
        return result
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValidationError) else type(exc).__name__
        with store.transaction() as con:
            con.execute("UPDATE experiments SET status='FAILED',error=? WHERE id=?", (message, trial_id))
            Store.audit(con, "EXPERIMENT_FAILED", {"id": trial_id, "error": message})
        raise
