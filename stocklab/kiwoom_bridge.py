"""Read-only integration with the unmodified official Kiwoom Python runtime.

No order, cancellation, transfer, or FX execution API is exposed here.
Credential and token material never enters the research database.
"""
from __future__ import annotations

import importlib.util
import re
import time

from .domain import ValidationError, now, symbol

OFFICIAL_REVISION = "953e5dbff123f437ab4d11a78a95191a685eb51f"
HOSTS = {"real": "https://api.kiwoom.com", "demo": "https://mockapi.kiwoom.com"}
READ_APIS = {"ka00001": "/api/dostk/acnt", "ka10001": "/api/dostk/stkinfo",
             "ka10004": "/api/dostk/mrkcond", "ka10080": "/api/dostk/chart", "ka10100": "/api/dostk/stkinfo",
             "kt00001": "/api/dostk/acnt", "kt00007": "/api/dostk/acnt", "kt00018": "/api/dostk/acnt",
             "usa06011": "/api/us/chart", "usa20100": "/api/us/mrkcond", "usa20101": "/api/us/mrkcond",
             "ust21070": "/api/us/acnt", "ust21110": "/api/us/acnt", "ust21160": "/api/us/acnt",
             "ust21180": "/api/us/acnt"}
# Newest-first chart APIs: only the first page is read; the result says so and is never "complete".
FIRST_PAGE_APIS = ("ka10080", "usa06011")


def _sdk():
    try:
        from kiwoom import KiwoomAuth, KiwoomClient
        from kiwoom.core.auth import get_base_url
        from kiwoom.core.secrets import ProfileKeyringSecretProvider, StaticSecretProvider
        from kiwoom.core.token_store import MemoryTokenStore
        return KiwoomAuth, KiwoomClient, get_base_url, ProfileKeyringSecretProvider, StaticSecretProvider, MemoryTokenStore
    except ImportError:
        raise ValidationError("키움 공식 클라이언트가 없습니다. README의 전용 환경 설치 절차를 실행하세요.") from None


def _mode(mode):
    if mode not in HOSTS:
        raise ValidationError("mode must be real or demo")
    return mode


def safe_error(exc):
    """Never echo upstream exception text, which could contain secrets or account data."""
    name = type(exc).__name__
    if name == "CredentialsNotFoundError":
        return "저장된 키가 없습니다. broker setup --mode real 또는 --mode demo로 연결하세요."
    safe_name = re.sub(r"[^A-Za-z0-9_]", "", name)[:80]
    return f"키움 연결 실패 ({safe_name}). 키의 실전/모의 구분, 등록 IP, 계좌 권한 및 네트워크를 확인하세요."


class KiwoomReadOnly:
    def __init__(self, mode: str, *, credentials=None):
        self.mode = _mode(mode)
        Auth, Client, base_url, Keyring, Static, Memory = _sdk()
        if base_url(mode) != HOSTS[mode]:
            raise ValidationError("PRD/MOCK endpoint override is not allowed; use the official Kiwoom host")
        self._base_url = base_url
        self._provider = Static(*credentials) if credentials else Keyring("stocklab-" + mode)
        self._auth = Auth(mode, self._provider, Memory(), profile="stocklab-" + mode, timeout_seconds=15)
        self._client = Client(self._auth, timeout_seconds=15)
        self._next_call = 0.0

    def close(self):
        self._client.session.close()
        self._auth.clear_token()

    def read(self, api_id: str, body: dict, *, max_pages=10, first_page_only=False):
        if api_id not in READ_APIS:
            raise ValidationError("This connection permits only the documented read APIs")
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 10:
            raise ValidationError("max_pages must be 1..10")
        if first_page_only and api_id not in FIRST_PAGE_APIS:
            raise ValidationError("Only newest-first chart reads may stop after the first page")
        pages, continuation = [], {}
        try:
            for _ in range(max_pages):
                if self._base_url(self.mode) != HOSTS[self.mode]:
                    raise ValidationError("Endpoint configuration changed")
                time.sleep(max(0, self._next_call - time.monotonic()))
                self._next_call = time.monotonic() + 1.1
                response = self._client.request(api_id=api_id, path=READ_APIS[api_id], body=body,
                                                extra_headers=continuation or None,
                                                retry_on_auth_failure=False)
                if not isinstance(response.body, dict) or response.body.get("return_code") not in (0, "0"):
                    raise ValidationError("Unconfirmed broker success response")
                pages.append(response.body)
                if first_page_only:
                    return {"broker": "kiwoom", "mode": self.mode, "read_only": True, "api_id": api_id,
                            "received_at": now(), "complete": False, "first_page_only": True, "pages": pages}
                if response.continuation.cont_yn != "Y":
                    return {"broker": "kiwoom", "mode": self.mode, "read_only": True, "api_id": api_id,
                            "received_at": now(), "complete": True, "pages": pages}
                if not response.continuation.next_key:
                    raise ValidationError("Broker continuation cursor missing")
                continuation = {"cont-yn": "Y", "next-key": response.continuation.next_key}
            raise ValidationError("조회가 10페이지를 초과했습니다. 불완전한 잔고를 사용하지 않습니다.")
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(safe_error(exc)) from None

    def accounts(self):
        return self.read("ka00001", {})

    def check(self, *, full=False):
        result = self.accounts()
        if not any(page.get("acctNo") for page in result["pages"]):
            raise ValidationError("인증 응답은 받았지만 연결된 계좌를 확인하지 못했습니다.")
        report = {"broker": "kiwoom", "mode": self.mode, "host": HOSTS[self.mode],
                "connection_verified": True, "read_only": True, "verified_at": result["received_at"],
                "account_reference_received": True}
        if full:
            report["checks"] = []
            for name, action in (("KR_quote", lambda: self.quote("KR", "005930")),
                                 ("US_quote", lambda: self.quote("US", "AAPL", "ND")),
                                 ("KR_balance", lambda: self.balance("KR")),
                                 ("US_balance", lambda: self.balance("US"))):
                try:
                    response = action()
                    report["checks"].append({"name": name, "ok": True, "pages": len(response["pages"]),
                                             "received_at": response["received_at"]})
                except Exception as exc:
                    report["checks"].append({"name": name, "ok": False, "error": safe_error(exc)})
            report["all_checks_passed"] = all(c["ok"] for c in report["checks"])
        return report

    def quote(self, market: str, ticker: str, exchange="ND"):
        symbol(ticker)
        if market == "KR":
            if not re.fullmatch(r"[0-9]{6}", ticker):
                raise ValidationError("국내 종목은 KRX 6자리 코드로 입력하세요.")
            return self.read("ka10001", {"stk_cd": ticker})
        if market != "US" or exchange not in ("ND", "NY", "NA"):
            raise ValidationError("미국 거래소는 ND(NASDAQ), NY(NYSE), NA(AMEX) 중 선택하세요.")
        return self.read("usa20100", {"stex_tp": exchange, "stk_cd": ticker})

    def balance(self, market: str):
        if market == "KR":
            return self.read("kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        if market == "US":
            return self.read("ust21070", {"stex_tp": "", "stk_cd": ""})
        raise ValidationError("Market must be KR or US")

    def cash(self, market: str):
        """Read broker cash and orderable amounts; no order API is available."""
        if market == "KR":
            return self.read("kt00001", {"qry_tp": "2"})
        if market == "US":
            return self.read("ust21110", {})
        raise ValidationError("Market must be KR or US")

    def us_deposit_detail(self):
        """ust21160 미국주식 예수금 상세 (includes the broker's USD 매도환율); read only."""
        return self.read("ust21160", {})

    def kr_orders(self, order_date: str, side: str, ticker: str):
        """kt00007 계좌별주문체결내역상세 for one KRX stock, one side, one order date (read only)."""
        if not re.fullmatch(r"[0-9]{8}", order_date or "") or side not in ("BUY", "SELL") \
                or not re.fullmatch(r"[0-9]{6}", ticker or ""):
            raise ValidationError("kt00007 조회 조건이 올바르지 않습니다.")
        return self.read("kt00007", {"ord_dt": order_date, "qry_tp": "1", "stk_bond_tp": "1",
                                     "sell_tp": "2" if side == "BUY" else "1", "stk_cd": ticker,
                                     "fr_ord_no": "", "dmst_stex_tp": "KRX"})

    def us_orders(self, start_date: str, end_date: str, side: str, exchange: str, ticker: str):
        """ust21180 미국주식 기간별 주문내역 for one symbol/side/exchange, ordinary (non-forced) orders (read only)."""
        if not re.fullmatch(r"[0-9]{8}", start_date or "") or not re.fullmatch(r"[0-9]{8}", end_date or "") \
                or start_date > end_date or side not in ("BUY", "SELL") or exchange not in ("ND", "NY", "NA") \
                or not re.fullmatch(r"[A-Z]{1,5}", ticker or ""):
            raise ValidationError("ust21180 조회 조건이 올바르지 않습니다.")
        return self.read("ust21180", {"strt_dt": start_date, "end_dt": end_date,
                                      "slby_tp": "2" if side == "BUY" else "1", "stex_tp": exchange,
                                      "stk_cd": ticker, "oppo_trde_tp": "0"})

    # ---- market data for the autonomous evidence snapshot (read only, no account data)

    def kr_minute_bars(self, ticker: str):
        """ka10080 1-minute bars, newest first; first page only."""
        if not re.fullmatch(r"[0-9]{6}", ticker or ""):
            raise ValidationError("국내 종목은 KRX 6자리 코드로 입력하세요.")
        return self.read("ka10080", {"stk_cd": ticker, "tic_scope": "1", "upd_stkpc_tp": "1"}, first_page_only=True)

    def kr_orderbook(self, ticker: str):
        """ka10004 주식호가 (best bid/ask and bid_req_base_tm)."""
        if not re.fullmatch(r"[0-9]{6}", ticker or ""):
            raise ValidationError("국내 종목은 KRX 6자리 코드로 입력하세요.")
        return self.read("ka10004", {"stk_cd": ticker})

    def kr_stock_info(self, ticker: str):
        """ka10100 종목정보 조회 (state, orderWarning, auditInfo)."""
        if not re.fullmatch(r"[0-9]{6}", ticker or ""):
            raise ValidationError("국내 종목은 KRX 6자리 코드로 입력하세요.")
        return self.read("ka10100", {"stk_cd": ticker})

    def us_minute_bars(self, exchange: str, ticker: str):
        """usa06011 1-minute bars in USD (exrt_appl_tp 0), newest first; first page only."""
        if exchange not in ("ND", "NY", "NA") or not re.fullmatch(r"[A-Z]{1,5}", ticker or ""):
            raise ValidationError("미국 거래소/티커가 올바르지 않습니다.")
        return self.read("usa06011", {"stex_tp": exchange, "stk_cd": ticker, "tic_scope": "1",
                                      "upd_stkpc_tp": "0", "exrt_appl_tp": "0"}, first_page_only=True)

    def us_orderbook(self, exchange: str, ticker: str):
        """usa20101 현재가 10호가 (fpr_sel_bid/fpr_buy_bid, dt + bid_tm)."""
        if exchange not in ("ND", "NY", "NA") or not re.fullmatch(r"[A-Z]{1,5}", ticker or ""):
            raise ValidationError("미국 거래소/티커가 올바르지 않습니다.")
        return self.read("usa20101", {"stex_tp": exchange, "stk_cd": ticker})


def status(mode):
    _mode(mode)
    installed = importlib.util.find_spec("kiwoom") is not None
    # Deliberately does not access credentials or issue a token.
    return {"broker": "kiwoom", "mode": mode, "sdk_installed": installed, "read_only": True,
            "host": HOSTS[mode], "connection_verified": False,
            "next_step": "python -m stocklab broker setup --mode " + mode,
            "credential_storage": "Windows Credential Manager / OS keyring", "token_storage": "memory only"}


def setup_window(mode):
    """Local masked input, user-initiated authentication, then OS credential storage."""
    _mode(mode)
    _, _, _, Keyring, _, _ = _sdk()
    import tkinter as tk
    from tkinter import ttk
    import threading
    import queue

    root = tk.Tk()
    root.title("Stock Lab · 키움 " + ("실전" if mode == "real" else "모의투자") + " 조회 연결")
    root.geometry("610x430")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="키움 실전계좌 조회 연결" if mode == "real" else "키움 모의투자 조회 연결", font=("Malgun Gothic", 17, "bold")).pack(anchor="w")
    ttk.Label(frame, text="인증·계좌 조회 후 이 PC의 자격 증명 저장소에 키를 저장합니다.\n매수·매도·환전 요청은 없습니다. 키 값은 채팅이나 로그로 전송하지 않습니다.", wraplength=550).pack(anchor="w", pady=(12, 18))
    ttk.Label(frame, text="App Key").pack(anchor="w")
    key = ttk.Entry(frame, show="•", width=78); key.pack(fill="x", pady=(4, 12))
    ttk.Label(frame, text="App Secret").pack(anchor="w")
    secret = ttk.Entry(frame, show="•", width=78); secret.pack(fill="x", pady=(4, 16))
    label = ttk.Label(frame, text="발급한 키의 환경이 " + mode + "인지 확인하세요.", wraplength=550)
    results = queue.Queue()

    def work(appkey, appsecret):
        client = None
        try:
            client = KiwoomReadOnly(mode, credentials=(appkey, appsecret))
            client.check()
            Keyring("stocklab-" + mode).set_credentials(mode, appkey, appsecret)
            results.put((True, "인증·계좌 조회 성공. 키를 이 PC의 자격 증명 저장소에 저장했습니다.\n이 창을 닫아도 됩니다. 실제 주문은 실행하지 않았습니다."))
        except Exception as exc:
            results.put((False, safe_error(exc)))
        finally:
            if client:
                client.close()

    def poll():
        try:
            success, message = results.get_nowait()
        except queue.Empty:
            root.after(100, poll); return
        label.configure(text=message)
        if not success:
            button.configure(state="normal")

    def submit():
        appkey, appsecret = key.get().strip(), secret.get().strip()
        if not appkey or not appsecret:
            label.configure(text="App Key와 App Secret을 모두 입력하세요."); return
        button.configure(state="disabled")
        key.delete(0, "end"); secret.delete(0, "end")
        label.configure(text="키움 " + mode + " 서버에 인증·계좌 조회 중…")
        threading.Thread(target=work, args=(appkey, appsecret), daemon=True).start()
        root.after(100, poll)

    button = ttk.Button(frame, text="연결 확인 후 이 PC에 저장", command=submit)
    button.pack(anchor="w")
    label.pack(anchor="w", pady=16)
    key.focus_set()
    def close_window():
        if button.instate(["disabled"]) and "성공" not in label.cget("text"):
            label.configure(text="연결·저장이 끝날 때까지 기다려 주세요.")
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close_window)
    root.mainloop()
