"""Local, read-only dashboard of the ACTUAL Kiwoom real account (account-wide figures).

Server-rendered HTML without JavaScript, bound to 127.0.0.1 only. Each page load
performs four documented read APIs through KiwoomReadOnly; there is no database,
simulation, quote or order path. Only allowlisted, normalized amounts are rendered:
no raw broker responses, account numbers, tokens, headers or exception text.
"""
from __future__ import annotations

from decimal import Decimal
import html
from http.server import BaseHTTPRequestHandler, HTTPServer

from .domain import ValidationError, decimal
from .kiwoom_bridge import KiwoomReadOnly

UNAVAILABLE = None
CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
       "connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
GENERIC_ERROR = "실계좌 조회를 완료하지 못했습니다. 연결 상태를 확인한 뒤 다시 조회하세요."


def _amount(value):
    """Normalize a broker amount string; anything missing or malformed is UNAVAILABLE, never zero."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return UNAVAILABLE
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return UNAVAILABLE
    try:
        return decimal(value, nonnegative=False)
    except ValidationError:
        return UNAVAILABLE


def _pages(result):
    """Pages of a read only if the bridge reported the response as complete."""
    if not isinstance(result, dict) or result.get("complete") is not True:
        return None
    pages = result.get("pages")
    if not isinstance(pages, list) or not pages or not all(isinstance(p, dict) for p in pages):
        return None
    return pages


def _summary_field(result, name):
    """Top-level field; every page that carries it must agree, otherwise UNAVAILABLE."""
    pages = _pages(result)
    if pages is None:
        return UNAVAILABLE
    values = [_amount(page[name]) for page in pages if name in page]
    if not values or UNAVAILABLE in values or len(set(values)) != 1:
        return UNAVAILABLE
    return values[0]


def _usd_row(result):
    """Exactly one crnc_code='USD' row across all pages of ust21110, else UNAVAILABLE."""
    pages = _pages(result)
    if pages is None:
        return UNAVAILABLE
    rows = []
    for page in pages:
        items = page.get("result_list")
        if items is None:
            continue
        if not isinstance(items, list):
            return UNAVAILABLE
        rows += [row for row in items if isinstance(row, dict) and row.get("crnc_code") == "USD"]
    return rows[0] if len(rows) == 1 else UNAVAILABLE


def _usd_valuation(result):
    pages = _pages(result)
    if pages is None:
        return UNAVAILABLE
    # ust21070 is documented as USD; refuse if the broker labels it otherwise.
    if any("crnc_code" in page and page["crnc_code"] != "USD" for page in pages):
        return UNAVAILABLE
    return _summary_field(result, "tot_evlt_amt")


def _read(client, method, market):
    try:
        result = getattr(client, method)(market)
    except Exception:
        return UNAVAILABLE, UNAVAILABLE
    received = result.get("received_at") if isinstance(result, dict) else None
    return result, received if isinstance(received, str) else UNAVAILABLE


def snapshot():
    """One client per request, always closed. Returns only allowlisted normalized values."""
    client = KiwoomReadOnly("real")
    try:
        kr_cash, kr_cash_at = _read(client, "cash", "KR")
        kr_bal, kr_bal_at = _read(client, "balance", "KR")
        us_cash, us_cash_at = _read(client, "cash", "US")
        us_bal, us_bal_at = _read(client, "balance", "US")
    finally:
        client.close()
    usd = _usd_row(us_cash)
    return [
        {"title": "국내주식 (KRW)", "currency": "KRW", "rows": [
            ("예수금", _summary_field(kr_cash, "entr")),
            ("주문가능금액", _summary_field(kr_cash, "ord_alow_amt")),
            ("보유종목 총평가금액", _summary_field(kr_bal, "tot_evlt_amt")),
        ], "reads": [("kt00001 예수금", kr_cash_at), ("kt00018 잔고", kr_bal_at)]},
        {"title": "미국주식 (USD)", "currency": "USD", "rows": [
            ("외화예수금", _amount(usd.get("fc_entra")) if usd else UNAVAILABLE),
            ("외화주문가능금액", _amount(usd.get("fc_ord_alowa")) if usd else UNAVAILABLE),
            ("보유종목 총평가금액", _usd_valuation(us_bal)),
        ], "reads": [("ust21110 예수금", us_cash_at), ("ust21070 잔고", us_bal_at)]},
    ]


def _money(value, currency):
    if value is UNAVAILABLE:
        return '<span class="na">조회 불가</span>'
    if currency == "USD" and value == value.quantize(Decimal("0.01")):
        text = f"{value:,.2f}"
    elif currency == "KRW" and value == value.to_integral_value():
        text = f"{value:,.0f}"
    else:
        text = f"{value:,f}"
    return html.escape(f"{text} {currency}")


def _when(value):
    if value is UNAVAILABLE:
        return '<span class="na">조회 실패 또는 불완전 응답</span>'
    return html.escape(value.replace("T", " ")[:19] + " UTC")


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>Stock Lab · 키움 실계좌 조회 (읽기 전용)</title>
<style>
body{{font-family:"Malgun Gothic",system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1b1b1b}}
.banner{{background:#7a0000;color:#fff;padding:14px 18px;border-radius:6px;font-weight:bold;font-size:1.1em}}
.plan{{border:2px dashed #b36b00;background:#fff8ec;padding:12px 16px;border-radius:6px;margin:16px 0}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px}}
th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left}} td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.na{{color:#a00;font-weight:bold}} .small{{color:#555;font-size:.9em}}
a.reload{{display:inline-block;padding:8px 16px;background:#1b4f9c;color:#fff;border-radius:4px;text-decoration:none}}
</style></head><body>
<div class="banner">실제 키움 실전계좌 · 읽기 전용 조회 · 주문 기능 없음 · 자동 매수·매도 없음</div>
{body}
<div class="plan"><b>실전 자동매매 한도는 별도 설정</b><br>
국내·미국에 정액을 배분하지 않습니다. 주문 수량은 실제 주문 가능 현금과 별도로 승인한 자본·주문 한도 안에서 계산합니다.
이 화면의 금액은 계좌 전체에 대한 증권사 조회값이며, 자동매매 설정값이나 허용 투자액을 뜻하지 않습니다.
이 화면은 손익·손실 기준을 감시하거나 주문을 집행하지 않습니다.</div>
<p><a class="reload" href="/">다시 조회</a> <span class="small">자동 새로고침 없음. 조회할 때마다 실계좌 조회 API를 호출합니다.</span></p>
<p class="small">표시 금액은 해당 통화 단위 그대로이며 환산하지 않습니다. 시각은 이 PC가 증권사 응답을 받은 시각(UTC)입니다.</p>
</body></html>"""


def render(sections):
    parts = []
    for section in sections:
        currency = section["currency"]
        rows = "".join(f'<tr><th>{html.escape(label)}</th><td class="num">{_money(value, currency)}</td></tr>'
                       for label, value in section["rows"])
        reads = " · ".join(f"{html.escape(name)}: {_when(at)}" for name, at in section["reads"])
        parts.append(f"<h2>{html.escape(section['title'])} — 계좌 전체</h2><table>{rows}</table>"
                     f'<p class="small">조회 수신 시각 — {reads}</p>')
    return PAGE.format(body="".join(parts))


def render_error():
    return PAGE.format(body=f'<p class="na">{html.escape(GENERIC_ERROR)}</p>')


def serve_real(port=8766):
    allowed_hosts = {"127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "stocklab"
        sys_version = ""
        timeout = 15

        def _send(self, status, text):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            # Replaces the stdlib error page so no request detail is echoed back.
            self.close_connection = True
            try:
                self._send(code, "<!doctype html><title>Stock Lab</title><p>요청을 처리할 수 없습니다.</p>")
            except Exception:
                pass

        def do_GET(self):
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1 or hosts[0].strip().lower() not in allowed_hosts:
                self.send_error(403)
                return
            if self.path != "/":
                self.send_error(404)
                return
            try:
                page, status = render(snapshot()), 200
            except Exception:
                page, status = render_error(), 503
            self._send(status, page)

        def _deny(self):
            self.send_error(405)

        do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _deny

        def log_message(self, *_args):
            pass

    class Server(HTTPServer):
        # Single-threaded on purpose: one broker read sequence at a time.
        def handle_error(self, request, client_address):
            pass

    server = Server(("127.0.0.1", port), Handler)
    print(f"Stock Lab REAL account (read-only): http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
