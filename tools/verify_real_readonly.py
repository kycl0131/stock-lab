"""Privacy-preserving LIVE Kiwoom read-only integration probe.

This script does not read secrets directly, place orders, call a model, or persist raw
broker responses. The JSON report contains only boolean results, API IDs, field names,
row counts, error categories and the time of the probe. Run during each market's
regular session to validate quote freshness; off-session failures are reported as such.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from kiwoom.core.errors import APIError
from stocklab.kiwoom_bridge import KiwoomReadOnly
from stocklab import live_evidence as ev, live_orders as lo, live_reconcile as rec
from stocklab.real_dashboard import _summary_field, _usd_row, _amount


def safe_error(exc: BaseException) -> str:
    """Do not persist upstream exception messages or anything that could contain account data."""
    if isinstance(exc, (ev.EvidenceError, rec.InquiryError)):
        code = re.sub(r"[^A-Za-z0-9_]", "", str(exc))[:80]
        return f"{type(exc).__name__}:{code}"
    return type(exc).__name__


def run() -> dict:
    report: dict = {"schema": "stocklab-real-readonly-probe-v1",
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                    "orders_sent": False, "raw_broker_responses_saved": False, "checks": {}}

    def record(name, operation):
        try:
            result = operation()
            report["checks"][name] = {"ok": True, **result}
        except BaseException as exc:
            report["checks"][name] = {"ok": False, "error": safe_error(exc)}

    client = KiwoomReadOnly("real")
    try:
        def kr_cash():
            result = client.cash("KR")
            a, b = _summary_field(result, "ord_alow_amt"), _summary_field(result, "100stk_ord_alow_amt")
            return {"api_id": "kt00001", "orderable_fields_valid": a is not None and b is not None,
                    "nonnegative": a is not None and b is not None and min(a, b) >= 0}
        record("kr_cash", kr_cash)

        def us_cash():
            result = client.cash("US")
            usd = _usd_row(result)
            valid = usd is not None and _amount(usd.get("fc_entra")) is not None \
                and _amount(usd.get("fc_ord_alowa")) is not None
            return {"api_id": "ust21110", "usd_row_valid": valid,
                    "list_field": "result_list" if any("result_list" in p for p in result["pages"]) else "absent"}
        record("us_cash", us_cash)

        record("us_fx", lambda: {"api_id": "ust21160", "fx_rate_valid":
                                    lo._fx_rate(client.us_deposit_detail()) is not None})

        def kr_holdings():
            result = client.balance("KR")
            rows = rec._rows(result, rec.KR_HOLDING_LIST)
            checked = 0
            for row in rows:
                raw = row.get("stk_cd")
                ticker = rec._kr_code(raw)
                if ticker:
                    rec.kr_holding(result, ticker)
                    checked += 1
            return {"api_id": "kt00018", "rows": len(rows), "parsed_rows": checked,
                    "list_field_present": any(rec.KR_HOLDING_LIST in p for p in result["pages"])}
        record("kr_holdings", kr_holdings)

        def us_holdings():
            result = client.balance("US")
            rows = rec.one_list(result, rec.US_ORDER_LISTS)
            checked = 0
            for row in rows:
                ticker = row.get("stk_cd")
                if isinstance(ticker, str) and re.fullmatch(r"[A-Z]{1,5}", ticker):
                    rec.us_holding(result, ticker)
                    checked += 1
            return {"api_id": "ust21070", "rows": len(rows), "parsed_rows": checked,
                    "list_field": next((n for n in rec.US_ORDER_LISTS if any(n in p for p in result["pages"])), "absent")}
        record("us_holdings", us_holdings)

        day_kst = datetime.now(timezone.utc).astimezone(ev.KST).strftime("%Y%m%d")
        day_utc = datetime.now(timezone.utc).strftime("%Y%m%d")

        def kr_history():
            result = client.kr_orders(day_kst, "BUY", "005930")
            rows = rec._rows(result, rec.KR_ORDER_LIST)
            return {"api_id": "kt00007", "rows": len(rows),
                    "list_field_present": any(rec.KR_ORDER_LIST in p for p in result["pages"]),
                    "sample_fields_present": bool(rows) and all(k in rows[0] for k in
                        ("ord_no", "ord_qty", "cntr_qty", "ord_remnq", "ord_tm"))}
        record("kr_order_history", kr_history)

        def us_history():
            body = {"strt_dt": day_utc, "end_dt": day_utc, "slby_tp": "2", "stex_tp": "ND",
                    "stk_cd": "AAPL", "oppo_trde_tp": "0"}
            try:
                response = client._client.request(api_id="ust21180", path="/api/us/acnt", body=body,
                                                  retry_on_auth_failure=False)
            except APIError as exc:
                if exc.return_code == 20 and any(word in exc.return_msg for word in
                                                  ("없습니다", "없음", "조회된 내역", "데이터가 없")):
                    return {"api_id": "ust21180", "no_data_code_20": True,
                            "rows": 0, "sample_fields_present": False}
                raise
            result = {"complete": response.continuation.cont_yn != "Y", "pages": [response.body]}
            rows = rec.one_list(result, rec.US_ORDER_LISTS)
            return {"api_id": "ust21180", "rows": len(rows),
                    "list_field": next((n for n in rec.US_ORDER_LISTS if any(n in p for p in result["pages"])), "absent"),
                    "sample_fields_present": bool(rows) and all(k in rows[0] for k in
                        ("ord_no", "ord_dt", "ord_qty", "cntr_qty", "ord_remnq", "ord_time"))}
        record("us_order_history", us_history)

        record("kr_bars_shape", lambda: {"api_id": "ka10080", "parsed_bars": len(ev.parse_kr_bars(
            client.kr_minute_bars("005930"), "005930", datetime.now(timezone.utc),
            lookback=5, max_age_s=172800)), "freshness_for_live_orders_checked": False})
        record("kr_status", lambda: {"api_id": "ka10100", "parse_ok":
                                    ev.parse_kr_status(client.kr_stock_info("005930"), "005930") == {}})
        record("kr_quote_freshness", lambda: {"api_id": "ka10004", "parse_ok": bool(
            ev.parse_kr_quote(client.kr_orderbook("005930"), "005930", datetime.now(timezone.utc), max_age_s=300))})

        record("us_bars_freshness", lambda: {"api_id": "usa06011", "parsed_bars": len(ev.parse_us_bars(
            client.us_minute_bars("ND", "AAPL"), "AAPL", datetime.now(timezone.utc),
            lookback=5, max_age_s=9000)[0])})
        record("us_quote_freshness", lambda: {"api_id": "usa20101", "parse_ok": bool(
            ev.parse_us_quote(client.us_orderbook("ND", "AAPL"), "AAPL", "ND",
                              datetime.now(timezone.utc), max_age_s=9000))})
        record("us_status", lambda: {"api_id": "usa20100", "parse_ok": bool(
            ev.parse_us_status(client.quote("US", "AAPL", "ND"), "AAPL", "ND"))})
    finally:
        client.close()
    return report


if __name__ == "__main__":
    result = run()
    path = Path("artifacts") / "live-readonly-validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
