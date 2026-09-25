"""Entirely synthetic fixtures. No real ticker or investment performance is implied."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .data import ingest
from .domain import ValidationError
from .engine import advance, initialize, run
from .paper_pilot import plan, setup, status
from .research import compare
from .store import Store

# Synthetic pilot fixture: nine weekday observations per market, whole-share prices that fit KRW 50,000.
# Observation 7 is a deliberate synthetic KR stress step so the loss gate is exercised. Not market data.
PILOT_FX_RATE, PILOT_FX_COST_BPS = "1400", "10"
PILOT_PRICES = {
    "KR": {"KR_PILOT_1": ["4000", "4020", "4040", "4060", "4080", "4100", "4090", "3300", "3320"],
           "KR_PILOT_2": ["2500", "2510", "2520", "2530", "2540", "2550", "2545", "2050", "2060"],
           "KR_PILOT_3": ["1500", "1505", "1510", "1515", "1520", "1525", "1530", "1540", "1545"]},
    "US": {"US_PILOT_1": ["3.40", "3.42", "3.44", "3.46", "3.48", "3.50", "3.49", "3.52", "3.53"],
           "US_PILOT_2": ["2.20", "2.21", "2.22", "2.23", "2.24", "2.25", "2.24", "2.26", "2.27"],
           "US_PILOT_3": ["5.00", "4.98", "4.96", "4.94", "4.92", "4.90", "4.88", "4.86", "4.85"]},
}


def build_demo(store: Store, *, reports="artifacts") -> dict:
    if any(store.state()["counts"].values()) or store.state()["accounts"]:
        raise ValidationError("Demo requires an empty database; choose a new --db path")
    prices, news, times = [], [], {"KR": [], "US": []}
    day = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for index in range(24):
        while day.weekday() > 4:
            day += timedelta(days=1)
        for mkt, hour, minute, base in (("KR", 6, 30, 20000), ("US", 20, 0, 80)):
            at = day.replace(hour=hour, minute=minute).isoformat()
            times[mkt].append(at)
            for number, drift in enumerate((Decimal("0.0004"), Decimal("-0.001"), Decimal("0.0002")), 1):
                wave = Decimal("0.0007") * (-1 if index % 2 else 1)
                price = Decimal(base + number * (1000 if mkt == "KR" else 5)) * (1 + drift * index + wave)
                ticker = f"{mkt}_DEMO_{number}"
                prices.append({"market": mkt, "symbol": ticker, "event_at": at, "available_at": at,
                               "price": str(price.quantize(Decimal("0.01"))), "volume": 500 if index % 3 else 2500,
                               "source": "synthetic-v2", "synthetic": True})
                if index in (6, 12, 18):
                    news.append({"market": mkt, "symbol": ticker, "published_at": at, "available_at": at,
                                 "headline": f"[가상 뉴스] {ticker} 연구용 발표 {index}",
                                 "body": "실제 기업 또는 실제 투자 정보가 아닌 합성 데이터입니다.",
                                 "source": "synthetic-v2", "synthetic": True})
        day += timedelta(days=1)
    ingest(store, prices, "prices")
    ingest(store, news, "news")
    for mkt, cash in (("KR", "3000000"), ("US", "2000")):
        initialize(store, mkt, cash)
        run(store, run_key=f"demo-{mkt}", mkt=mkt, as_of=times[mkt][6], strategy="momentum", mode="paper")
        advance(store, mkt, times[mkt][7])
        compare(store, mkt, reports)
    return {"message": "Synthetic demo ready. All orders and balances are simulated.", "database": str(store.path),
            "counts": store.state()["counts"], "dashboard": f'python -m stocklab --db "{store.path}" serve'}


def build_pilot_demo(store: Store) -> dict:
    """SYNTHETIC two-market pilot walk-through: controls only, not a backtest, no alpha implied."""
    setup(store, plan(fx_rate=PILOT_FX_RATE, fx_cost_bps=PILOT_FX_COST_BPS))
    days, day = [], datetime(2026, 8, 3, tzinfo=timezone.utc)
    while len(days) < 9:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    times = {"KR": [d.replace(hour=6, minute=30).isoformat() for d in days],
             "US": [d.replace(hour=20, minute=0).isoformat() for d in days]}
    ingest(store, [{"market": mkt, "symbol": ticker, "event_at": times[mkt][i], "available_at": times[mkt][i],
                    "price": price, "volume": 100000, "source": "synthetic-pilot-v1", "synthetic": True}
                   for mkt, series in PILOT_PRICES.items() for ticker, prices in series.items()
                   for i, price in enumerate(prices)], "prices")
    steps = []

    def paper_run(mkt, index, key, note):
        result = run(store, run_key=key, mkt=mkt, as_of=times[mkt][index], mode="paper",
                     strategy="momentum", lookback=5, top_k=2, weight="0.20")
        steps.append({"step": note, "market": mkt, "as_of": times[mkt][index], "run_key": key, "status": result["status"]})

    def paper_advance(mkt, index, note):
        fills = advance(store, mkt, times[mkt][index])
        steps.append({"step": note, "market": mkt, "as_of": times[mkt][index], "fills": len(fills)})

    # Strict chronological interleaving: KR closes (06:30Z) before the same day's US close (20:00Z).
    paper_run("KR", 5, "pilot-demo-KR-1", "momentum baseline orders")
    paper_run("US", 5, "pilot-demo-US-1", "momentum baseline orders")
    paper_advance("KR", 6, "fills on the next observation")
    paper_advance("US", 6, "fills on the next observation")
    paper_advance("KR", 7, "SYNTHETIC KR stress step: marks drop, KR planned-loss threshold latches")
    paper_run("KR", 7, "pilot-demo-KR-2", "rebalance under latch: new buys rejected, risk-reducing sells allowed")
    paper_advance("US", 7, "mark only")
    paper_advance("KR", 8, "KR sells fill; no automatic liquidation was involved")
    paper_advance("US", 8, "mark only")
    state = store.state()
    return {"notice": "SYNTHETIC FIXTURE. Invented symbols, prices and FX (1400 KRW/USD, 10 bps). Demonstrates "
                      "paper controls only; it is not market data, not a backtest and implies no alpha.",
            "database": str(store.path), "steps": steps,
            "orders": [{k: o[k] for k in ("market", "symbol", "side", "quantity", "filled", "status", "reason", "created_at")}
                       for o in sorted(state["orders"], key=lambda o: (o["created_at"], o["market"], o["symbol"]))],
            "status": status(store, times["US"][8]),
            "next": [f'python -m stocklab --db "{store.path}" pilot-status',
                     f'python -m stocklab --db "{store.path}" serve']}
