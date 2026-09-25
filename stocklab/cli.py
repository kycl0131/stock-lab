import argparse
import json
from pathlib import Path
import sys
import sqlite3

from .data import import_csv
from .demo import build_demo, build_pilot_demo
from .domain import RiskPolicy, ValidationError, digest
from .engine import abandon, advance, cancel, context, halt, initialize, run
from .paper_pilot import plan as pilot_plan, setup as pilot_setup, status as pilot_status
from .pilot import scenario
from .research import compare
from .server import serve
from .store import Store


def parser():
    cli = argparse.ArgumentParser(description="Stock Lab: KR/US point-in-time research, paper trading, human-confirmed REAL "
                                              "limit-order tickets (`live`) and an armed autonomous runner (`auto`; "
                                              "inactive until configured and armed)")
    cli.add_argument("--db", default="data/stocklab.db", help="SQLite database; use a different file for each account/experiment")
    sub = cli.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo", help="Create synthetic KR/US data, simulated orders and baseline comparison reports")
    p.add_argument("--reports", default="artifacts")
    p = sub.add_parser("init", help="Initialize a paper account")
    p.add_argument("--market", choices=["KR", "US"], required=True)
    p.add_argument("--cash", required=True)
    p.add_argument("--policy", help="Optional JSON RiskPolicy file; values are illustrative paper controls")
    p = sub.add_parser("import", help="Import explicit-timestamp CSV data")
    p.add_argument("kind", choices=["prices", "news"])
    p.add_argument("path")
    p = sub.add_parser("snapshot", help="Export the exact evidence consumed by a decision")
    p.add_argument("--market", choices=["KR", "US"], required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--lookback", type=int, default=5)
    p.add_argument("--visibility", choices=["replay", "recorded"], default="replay")
    p = sub.add_parser("run", help="Record one idempotent decision and optionally paper orders")
    p.add_argument("--market", choices=["KR", "US"], required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--key", required=True, help="Stable scheduler/run key; repeated keys never repeat inference or orders")
    p.add_argument("--mode", choices=["shadow", "paper"], default="shadow")
    p.add_argument("--strategy", choices=["momentum", "equal", "file", "anthropic"], default="momentum")
    p.add_argument("--lookback", type=int, default=5)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--weight", default="0.20")
    p.add_argument("--visibility", choices=["replay", "recorded"], default="replay")
    p.add_argument("--decision-file")
    p.add_argument("--model")
    p = sub.add_parser("advance", help="Process subsequent observations in the paper account")
    p.add_argument("--market", choices=["KR", "US"], required=True)
    p.add_argument("--as-of", required=True)
    p = sub.add_parser("cancel", help="Cancel remaining simulated quantity")
    p.add_argument("order_id")
    p = sub.add_parser("abandon", help="Mark an interrupted PENDING run failed; its key stays consumed")
    p.add_argument("--key", required=True)
    p.add_argument("--reason", required=True)
    for cmd in ("halt", "resume"):
        p = sub.add_parser(cmd)
        p.add_argument("--market", choices=["KR", "US"], required=True)
        p.add_argument("--reason", required=True)
    p = sub.add_parser("compare", help="Run fixed equal/momentum baseline comparisons; records all trial databases")
    p.add_argument("--market", choices=["KR", "US"], required=True)
    p.add_argument("--output", default="artifacts")
    p.add_argument("--lookback", type=int, default=5)
    sub.add_parser("status")
    p = sub.add_parser("broker", help="Kiwoom official SDK: authentication and read-only queries")
    b = p.add_subparsers(dest="broker_command", required=True)
    for command in ("setup", "status", "check", "accounts", "quote", "balance"):
        q = b.add_parser(command)
        q.add_argument("--mode", choices=["real", "demo"], required=True)
        if command == "check":
            q.add_argument("--full", action="store_true", help="Also verify KR/US quote and balance reads without printing account contents")
        if command in ("quote", "balance"):
            q.add_argument("--market", choices=["KR", "US"], required=True)
        if command == "quote":
            q.add_argument("--symbol", required=True)
            q.add_argument("--exchange", choices=["ND", "NY", "NA"], default="ND")
    p = sub.add_parser("pilot", help="시장별 5만원 이하 소액 시범 매매의 비용·손실 시나리오 (오프라인 계산, 주문·조회 없음)",
                       description="입력값만으로 계산하는 계획용 보고서입니다. DB·네트워크·키 저장소·증권사를 사용하지 않으며 종목 추천이 아닙니다.")
    p.add_argument("--market", choices=["KR", "US"], required=True, help="KR: 원화 단가, US: 달러 단가")
    p.add_argument("--price", required=True, help="검토할 1주 단가 (KR 원 정수, US 달러). 시세를 조회하지 않으므로 직접 입력")
    p.add_argument("--quantity", required=True, help="정수 수량 (1 이상)")
    p.add_argument("--buy-commission-bps", required=True, help="본인 계좌의 실제 매수 수수료율, bps (예: 0.015%% = 1.5). 기본값 없음")
    p.add_argument("--sell-commission-bps", required=True, help="본인 계좌의 실제 매도 수수료율, bps")
    p.add_argument("--sell-tax-bps", required=True, help="해당 상품의 매도 거래세율, bps. 적용되지 않으면 0을 명시")
    p.add_argument("--slippage-bps", required=True, help="편도 가격 미끄러짐 가정, bps. 매수·매도에 각각 적용")
    p.add_argument("--max-loss-krw", required=True, help="계획 손실 기준(원). 시장별 2500원 이하; 실제 손실 상한을 보장하지 않음")
    p.add_argument("--adverse-move-pct", required=True, help="점검할 불리한 가격 하락률 1개, %% (0 초과 100 이하)")
    p.add_argument("--fx-rate", help="US 필수: 직접 입력한 원/달러 환율 (자동 조회 없음)")
    p.add_argument("--fx-cost-bps", help="US 필수: 환전 비용 가정, bps. 원→달러·달러→원 각각 적용")
    p.add_argument("--budget-krw", default="50000", help="시장별 한도(원). 기본 50000, 더 작은 값만 허용")
    p = sub.add_parser("pilot-setup", help="New DB only: local KR+US paper pilot, KRW 50,000 per market (no broker, no mock server)")
    p.add_argument("--fx-rate", required=True, help="USD/KRW rate you supply (KRW per USD); static for the whole pilot, never fetched")
    p.add_argument("--fx-cost-bps", required=True, help="Assumed cost per KRW<->USD conversion, bps; charged on setup and on valuation")
    p.add_argument("--kr-policy", help="Optional JSON RiskPolicy for KR; max_order_notional must be <= 50000")
    p.add_argument("--us-policy", help="Optional JSON RiskPolicy for US; max_order_notional must be <= the USD pilot cash")
    p = sub.add_parser("pilot-status", help="Paper pilot valuation per market and combined in KRW (paper quotes only)")
    p.add_argument("--as-of", help="Valuation time; default is the later market clock. Earlier than a clock is refused")
    sub.add_parser("pilot-demo", help="New DB only: SYNTHETIC two-market pilot walk-through (not market data, no alpha)")
    p = sub.add_parser("serve", help="Paper research artifact: local read-only dashboard of a SQLite paper/demo DB")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("serve-real", help="Local read-only dashboard of the ACTUAL Kiwoom real account (account-wide; no orders)",
                       description="Reads account-wide KR/US cash and holdings valuation via the Kiwoom read APIs on each page load. "
                                   "No DB, no simulation, no order functionality. Binds 127.0.0.1 only.")
    p.add_argument("--port", type=int, default=8766)
    p = sub.add_parser("live", help="REAL-MONEY Kiwoom limit-order tickets with typed human confirmation",
                       description="실전 계좌 주문 티켓. prepare는 전송하지 않고(오프라인), send만 확인 문구 입력 후 "
                                   "주문 1건을 전송합니다. reconcile은 주문·체결 조회(KR kt00007, US ust21180, 읽기 전용)입니다. "
                                   "재전송·취소 없음. US 대사는 실제 응답으로 미검증. 자세한 내용은 README의 live 절.")
    lv = p.add_subparsers(dest="live_command", required=True)
    q = lv.add_parser("prepare", help="5분 유효 티켓 생성 (주문 전송·네트워크 없음)")
    q.add_argument("--market", choices=["KR", "US"], required=True)
    q.add_argument("--side", choices=["BUY", "SELL"], required=True,
                   help="BUY는 `live cap` 설정 필요. SELL은 이 프로그램의 확인된 매수 체결 수량 이내")
    q.add_argument("--symbol", required=True, help="KR: 6자리 코드, US: 대문자 티커")
    q.add_argument("--exchange", choices=["KRX", "ND", "NY", "NA"], help="KR은 KRX(기본), US는 ND/NY/NA 필수")
    q.add_argument("--quantity", required=True, help="정수 주식 수")
    q.add_argument("--limit-price", required=True, help="지정가. KR 원 정수(호가단위), US 달러 센트 단위")
    q = lv.add_parser("send", help="확인 문구 입력 후 실전 주문 1건 전송 (티켓당 1회, 재전송 없음)")
    q.add_argument("--ticket", required=True)
    q = lv.add_parser("reconcile", help="전송된 주문 1건의 체결을 KR kt00007 / US ust21180으로 조회해 확인된 체결만 기록 (읽기 전용)")
    q.add_argument("--ticket", required=True)
    q.add_argument("--order-no", help="결과불명 티켓: 키움 앱/HTS에 보이는 주문번호. 조건·시각이 유일하게 일치할 때만 연결")
    q = lv.add_parser("close", help="앱에서 잔량 없음을 확인한 뒤 사람이 티켓 종결 (확인 문구, 전송·취소·재전송 없음)")
    q.add_argument("--ticket", required=True)
    q = lv.add_parser("cap", help="시장별 BUY 한도 명시 설정 (확인 문구, 이력 보존). 미설정 시 BUY 차단")
    q.add_argument("--market", choices=["KR", "US"], required=True)
    q.add_argument("--max-committed-krw", required=True, help="누적 약정 한도(원, 레거시 예약 포함, 매도로 늘지 않음)")
    q.add_argument("--max-order-krw", required=True, help="주문 1건 예약금 한도(원)")
    q.add_argument("--cash-fraction-pct", required=True, help="전송 시 실제 주문가능현금 대비 주문당 최대 비율(0~100)")
    q.add_argument("--reason", required=True)
    q = lv.add_parser("status", help="티켓·한도·시범 보유·중지 상태 (오프라인, 주문번호 마스킹)")
    q.add_argument("--ticket")
    q = lv.add_parser("halt", help="영구 중지: prepare/send 차단. 해제 기능 없음, 자동 청산 없음")
    q.add_argument("--market", choices=["KR", "US", "ALL"], required=True)
    q.add_argument("--reason", required=True)
    p = sub.add_parser("auto", help="REAL-MONEY autonomous runner: config / arm / run-once / watch / status (inactive until armed)",
                       description="무인 AI 자동매매 실행기. 코드가 있다는 것과 켜져 있다는 것은 다릅니다. 설정 저장(`auto config`)과 "
                                   "확인 문구를 입력한 arm(`auto arm`)이 모두 유효할 때만 실제 주문을 전송합니다. `--dry-run`은 "
                                   "판단·가상 주문만 기록합니다. 자세한 내용은 README의 auto 절.")
    au = p.add_subparsers(dest="auto_command", required=True)
    au.add_parser("template", help="설정 JSON 틀 출력 (값은 모두 null, 기본값 없음; 오프라인)")
    q = au.add_parser("config", help="설정 파일 검증 후 저장 (확인 문구, 이력 보존, 기존 arm 무효화; 오프라인)")
    q.add_argument("--file", required=True)
    q.add_argument("--reason", required=True)
    q = au.add_parser("arm", help="최신 설정·한도에 묶인 자동 주문 권한을 N시간 부여 (확인 문구; 오프라인)")
    q.add_argument("--hours", required=True, type=int)
    q.add_argument("--reason", required=True)
    q = au.add_parser("disarm", help="자동 주문 권한 즉시 철회 (확인 불필요; 오프라인)")
    q.add_argument("--reason", required=True)
    q = au.add_parser("run-once", help="장중 창이면 시장별 1회 실행 (조회 API 사용; arm 없이 --dry-run 가능)")
    q.add_argument("--dry-run", action="store_true", help="주문 없이 판단·가상 주문만 기록")
    q.add_argument("--market", choices=["KR", "US"])
    q = au.add_parser("watch", help="간격마다 run-once 반복 (단일 실행 잠금; LIVE는 권한이 사라지면 종료)")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--max-cycles", type=int)
    au.add_parser("status", help="권한·설정·최근 판단·주문 의도·이벤트 (오프라인)")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "broker":
            from .kiwoom_bridge import KiwoomReadOnly, setup_window, status, safe_error
            if args.broker_command == "setup":
                setup_window(args.mode)
                return
            if args.broker_command == "status":
                result = status(args.mode)
            else:
                client = None
                try:
                    client = KiwoomReadOnly(args.mode)
                    if args.broker_command == "check":
                        result = client.check(full=args.full)
                    elif args.broker_command == "quote":
                        result = client.quote(args.market, args.symbol, args.exchange)
                    elif args.broker_command == "balance":
                        result = client.balance(args.market)
                    else:
                        result = client.accounts()
                except ValidationError:
                    raise
                except Exception as exc:
                    raise ValidationError(safe_error(exc)) from None
                finally:
                    if client:
                        client.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "serve-real":
            # Handled before Store so no database file is created or opened.
            if not 1024 <= args.port <= 65535:
                raise ValidationError("Choose a port in 1024..65535")
            from .real_dashboard import serve_real
            serve_real(args.port)
            return
        if args.command == "live":
            # Separate live-order DB; handled before Store so the paper/research DB is never opened.
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
            from . import live_orders
            conn = live_orders.open_db()
            try:
                if args.live_command == "prepare":
                    result = live_orders.prepare(conn, market=args.market, side=args.side, symbol=args.symbol,
                                                 exchange=args.exchange, quantity=args.quantity,
                                                 limit_price=args.limit_price)
                elif args.live_command == "send":
                    result = live_orders.send(conn, args.ticket)
                elif args.live_command == "reconcile":
                    result = live_orders.reconcile(conn, args.ticket, args.order_no)
                elif args.live_command == "close":
                    result = live_orders.close(conn, args.ticket)
                elif args.live_command == "cap":
                    result = live_orders.set_cap(conn, market=args.market, max_committed_krw=args.max_committed_krw,
                                                 max_order_krw=args.max_order_krw,
                                                 cash_fraction_pct=args.cash_fraction_pct, reason=args.reason)
                elif args.live_command == "halt":
                    result = live_orders.halt(conn, args.market, args.reason)
                else:
                    result = live_orders.status(conn, args.ticket)
            finally:
                conn.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.live_command == "send" and result.get("state") != "ACCEPTED":
                raise SystemExit(3)
            return
        if args.command == "auto":
            # Same per-user live ledger; the paper/research DB is never opened.
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
            from . import live_auto, live_orders
            if args.auto_command == "template":
                print(json.dumps(live_auto.template(), ensure_ascii=False, indent=2))
                return
            if args.auto_command == "watch" and args.max_cycles is not None and args.max_cycles < 1:
                raise ValidationError("--max-cycles는 1 이상입니다.")
            conn = live_orders.open_db()
            try:
                if args.auto_command == "config":
                    result = live_auto.set_config(conn, args.file, args.reason)
                elif args.auto_command == "arm":
                    result = live_auto.arm(conn, args.hours, args.reason)
                elif args.auto_command == "disarm":
                    result = live_auto.disarm(conn, args.reason)
                elif args.auto_command == "run-once":
                    result = live_auto.run_once(conn, dry_run=args.dry_run, market=args.market)
                elif args.auto_command == "watch":
                    try:
                        result = live_auto.watch(conn, dry_run=args.dry_run, max_cycles=args.max_cycles)
                    except KeyboardInterrupt:
                        result = {"stopped": "KEYBOARD_INTERRUPT"}
                else:
                    result = live_auto.status(conn)
            finally:
                conn.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "pilot":
            # Pure local arithmetic: handled before Store so no database file is created.
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
            result = scenario(market_code=args.market, price=args.price, quantity=args.quantity,
                              buy_commission_bps=args.buy_commission_bps, sell_commission_bps=args.sell_commission_bps,
                              sell_tax_bps=args.sell_tax_bps,
                              slippage_bps=args.slippage_bps, max_loss_krw=args.max_loss_krw,
                              adverse_move_pct=args.adverse_move_pct, fx_rate=args.fx_rate,
                              fx_cost_bps=args.fx_cost_bps, budget_krw=args.budget_krw)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command.startswith("pilot-"):
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
        prepared = None
        if args.command in ("pilot-setup", "pilot-demo"):
            # Checked before Store() so an existing real/demo database file is never touched.
            path = Path(args.db)
            if (path.exists() and path.stat().st_size > 0) or Path(f"{path}-wal").exists():
                raise ValidationError(f"{args.command} requires a new --db path; existing database files are never modified")
        if args.command == "pilot-setup":
            # Validate every input before a database file is created.
            policies = {mkt: RiskPolicy(**json.loads(Path(file).read_text(encoding="utf-8-sig")))
                        for mkt, file in (("KR", args.kr_policy), ("US", args.us_policy)) if file}
            prepared = pilot_plan(fx_rate=args.fx_rate, fx_cost_bps=args.fx_cost_bps, policies=policies)
        if args.command == "pilot-status" and not Path(args.db).exists():
            raise ValidationError("Database not found; create a pilot with pilot-setup first")
        store = Store(args.db)
        if args.command == "demo":
            result = build_demo(store, reports=args.reports)
        elif args.command == "pilot-setup":
            result = pilot_setup(store, prepared)
        elif args.command == "pilot-status":
            result = pilot_status(store, args.as_of)
        elif args.command == "pilot-demo":
            result = build_pilot_demo(store)
        elif args.command == "init":
            policy = RiskPolicy(**json.loads(Path(args.policy).read_text(encoding="utf-8-sig"))) if args.policy else None
            initialize(store, args.market, args.cash, policy)
            result = store.account(args.market)
        elif args.command == "import":
            result = {"inserted": import_csv(store, args.path, args.kind)}
        elif args.command == "snapshot":
            data = context(store, args.market, args.as_of, lookback=args.lookback, visibility=args.visibility)
            result = {"snapshot_hash": digest(data), "snapshot": data}
        elif args.command == "run":
            result = run(store, run_key=args.key, mkt=args.market, as_of=args.as_of, mode=args.mode,
                         strategy=args.strategy, lookback=args.lookback, top_k=args.top_k, weight=args.weight,
                         visibility=args.visibility, decision_file=args.decision_file, model=args.model)
            result.pop("snapshot", None)
        elif args.command == "advance":
            result = {"fills": advance(store, args.market, args.as_of)}
        elif args.command == "cancel":
            cancel(store, args.order_id)
            result = {"status": "cancel processed"}
        elif args.command == "abandon":
            abandon(store, args.key, args.reason)
            result = {"status": "interrupted run abandoned"}
        elif args.command in ("halt", "resume"):
            halt(store, args.market, args.reason, resume=args.command == "resume")
            result = store.account(args.market)
        elif args.command == "compare":
            result = compare(store, args.market, args.output, lookback=args.lookback)
        elif args.command == "serve":
            if not 1024 <= args.port <= 65535:
                raise ValidationError("Choose a port in 1024..65535")
            serve(store, args.port)
            return
        else:
            result = store.state()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "run" and result["status"] != "COMPLETED":
            raise SystemExit(2)
        if args.command == "pilot-status" and result["combined"]["valuation"] != "AVAILABLE":
            raise SystemExit(2)
    except (ValidationError, OSError, json.JSONDecodeError, TypeError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
