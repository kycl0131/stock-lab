# Stock Lab

개인 한국·미국 주식 연구 도구입니다. 현재 사용하는 화면은 **키움 실전 계좌의 실제 잔고를 조회 전용으로 표시**합니다. 실전 주문은 두 경로뿐입니다. (1) CLI `live`: **사람이 확인 문구를 직접 입력한 지정가 주문 1건씩**(아래 "실전 주문 티켓"). (2) CLI `auto`: **무인 자동 실행기**. 설정 저장과 확인 문구를 입력한 arm이 모두 유효할 때만 같은 원장 게이트를 거쳐 주문합니다(아래 "무인 자동 실행"). **코드가 있다는 것은 켜져 있다는 뜻이 아닙니다.** 설정·arm 전에는 주문하지 않으며, 이 저장소에서 자동 실행은 한 번도 켜지거나 실행되지 않았습니다. 매수는 명시 한도 안에서만, 매도는 이 프로그램이 산 확인된 체결 수량만 가능합니다. 재전송은 없고, 대시보드는 계속 조회 전용입니다. 과거 로컬 모의 기능은 연구용으로 남아 있습니다.

## 지금 시작: 키움 실전 계좌 연결

1. [키움 REST API 포털](https://openapi.kiwoom.com/main/home)에서 실전 API 사용신청·계좌 연결·키 발급을 완료합니다.
2. 등록 IP가 실행 PC의 공인 IP인지 확인합니다. 키 다운로드가 1회로 제한된 경우 파일을 보관합니다.
3. 이 폴더에서 아래 명령으로 로컬 입력창을 엽니다. **키를 채팅이나 명령행 인수에 넣지 않습니다.**

```powershell
.\.venv\Scripts\python.exe -m stocklab broker setup --mode real
```

입력창의 연결 버튼을 누르면 공식 서버에서 인증·계좌 조회를 수행한 뒤, 키를 OS 자격 증명 저장소의 Stock Lab 전용 항목에 저장합니다. 기존 키움 CLI 프로필은 변경하지 않습니다. 접근 토큰은 프로세스 메모리에만 보관합니다. 저장 시 같은 Stock Lab 환경 항목은 갱신됩니다.

연결 후 조회:

```powershell
.\.venv\Scripts\python.exe -m stocklab broker check --mode real --full
.\.venv\Scripts\python.exe -m stocklab broker quote --mode real --market KR --symbol 005930
.\.venv\Scripts\python.exe -m stocklab broker quote --mode real --market US --symbol AAPL --exchange ND
.\.venv\Scripts\python.exe -m stocklab broker balance --mode real --market KR
.\.venv\Scripts\python.exe -m stocklab broker balance --mode real --market US
```

### 실계좌 대시보드: `serve-real` (현재 사용하는 화면)

```powershell
.\.venv\Scripts\python.exe -m stocklab serve-real
```

브라우저: http://127.0.0.1:8766 (`--port`로 변경 가능)

> **표시 금액은 계좌 전체의 실제 증권사 조회값입니다.** 국내·미국에 고정 금액을 배분하지 않습니다. 실전 매수 한도는 별도로 명시한 `live cap`과 `auto config`만 사용합니다. 별도로 입금된 하위계좌가 아니고, 이 화면은 손익이나 손실 기준을 감시·집행하지 않습니다. **이 조회 화면에는 주문 기능이 없고 자동 매수·매도도 없습니다.**

- 페이지를 열거나 "다시 조회"를 누를 때마다 기존 OS 자격 증명 저장소의 실전 키로 공식 조회 API 4개를 호출합니다: 국내 `kt00001`(예수금 `entr`, 주문가능금액 `ord_alow_amt`, KRW), `kt00018`(보유 총평가금액 `tot_evlt_amt`, KRW), 미국 `ust21110`(`crnc_code=USD` 행의 외화예수금 `fc_entra`, 외화주문가능금액 `fc_ord_alowa`, USD), `ust21070`(보유 총평가금액 `tot_evlt_amt`, USD). 시세·주문 API, DB, 시뮬레이션은 사용하지 않습니다. 자동 새로고침은 없습니다.
- 금액은 각 통화 그대로 표시하며 환산·합산하지 않습니다. 필드가 없거나 형식이 잘못되었거나, USD 행이 없거나, 여러 페이지 응답이 완결되지 않으면 0이 아닌 **조회 불가**로 표시합니다. 표시 시각은 이 PC가 증권사 응답을 받은 시각(UTC)입니다.
- 원본 응답·계좌번호·토큰·헤더·오류 원문은 화면에 표시하지 않습니다. 연결 실패 시 일반 안내 문구만 표시합니다.
- 127.0.0.1에만 바인딩하고 Host가 `127.0.0.1[:포트]`/`localhost[:포트]`가 아니면 거부합니다. `GET /` 외 경로·메서드는 거부합니다. JavaScript·외부 리소스 금지(CSP), `Cache-Control: no-store`, 요청 로그 없음. 화면에 실제 잔고가 보이므로 화면 공유·캡처에 주의하세요.

`broker status --mode real`은 SDK 설치와 설정 안내만 보여주는 **오프라인 상태 조회**입니다. 인증 성공 여부를 검사하지 않습니다. 조회 명령은 계좌 정보를 터미널에 표시하므로 출력물을 공유하지 마세요. 증권사 응답은 연구 DB에 자동 복사하지 않습니다. 현재가 응답의 누적 거래량을 체결 가능한 거래량으로 간주하지 않습니다.

### 실전 주문 티켓: `live` (실제 돈, 사람 확인 필수)

> **실제 계좌에 실제 주문을 보내는 기능입니다.** 사람이 한 건씩 확인하는 경로이며 수익을 보장하지 않습니다. 오프라인 안전 검사와 실계좌 읽기 전용 조회는 확인했지만, 실제 주문 전송·체결·대사는 아직 검증하지 않았습니다. 검증 범위는 [VALIDATION.md](VALIDATION.md)에 기록했습니다.

```powershell
# 0) 시장별 BUY 한도를 직접 정함(오프라인, 확인 문구, 이력 보존). 설정 전에는 BUY가 차단됨. 기본값 없음
.\.venv\Scripts\python.exe -m stocklab live cap --market KR --max-committed-krw <원> --max-order-krw <원> --cash-fraction-pct <0~100> --reason "사유"

# 1) 티켓 준비: 오프라인(네트워크·키 저장소·증권사 없음). 5분 유효. 주문을 보내지 않음
.\.venv\Scripts\python.exe -m stocklab live prepare --market KR --side BUY --symbol 005930 --quantity 1 --limit-price 10000
.\.venv\Scripts\python.exe -m stocklab live prepare --market US --side BUY --symbol AAPL --exchange ND --quantity 1 --limit-price 12.34
.\.venv\Scripts\python.exe -m stocklab live prepare --market KR --side SELL --symbol 005930 --quantity 1 --limit-price 10000

# 2) 전송: 화면에 나온 확인 문구를 직접 입력해야 주문 1건 전송
.\.venv\Scripts\python.exe -m stocklab live send --ticket LT-20260925-XXXXXXXXXXXX

# 3) 주문·체결 대사(읽기 전용: 국내 kt00007, 미국 ust21180). 결과불명 티켓은 앱에 보이는 주문번호를 --order-no로
.\.venv\Scripts\python.exe -m stocklab live reconcile --ticket LT-20260925-XXXXXXXXXXXX
.\.venv\Scripts\python.exe -m stocklab live reconcile --ticket LT-20260925-XXXXXXXXXXXX --order-no 1234567

# 4) 앱에서 잔량이 없음을 확인한 뒤 사람이 종결(확인 문구, 전송·취소·재전송 없음)
.\.venv\Scripts\python.exe -m stocklab live close --ticket LT-20260925-XXXXXXXXXXXX

# 상태(오프라인), 영구 중지
.\.venv\Scripts\python.exe -m stocklab live status
.\.venv\Scripts\python.exe -m stocklab live status --ticket LT-20260925-XXXXXXXXXXXX
.\.venv\Scripts\python.exe -m stocklab live halt --market ALL --reason "수동 중지"
```

위 종목·가격은 명령 형식 예시일 뿐 추천이나 실제 시세가 아닙니다. 실전 주문 기록은 이 PC의 사용자 프로필 `%LOCALAPPDATA%\StockLab\live-orders.db`에 저장합니다. 다른 작업 폴더·복사본에서 실행해도 같은 기록을 사용하며, 모의/연구 DB는 열지 않습니다. 이 파일을 삭제하거나 다른 PC에서 실행하면 이전 주문 이력을 알 수 없으므로, 그런 경우 증권사 주문·체결 내역과 대사하기 전에는 새 주문을 준비하지 마세요.

**주문 범위**: 국내 KRX·미국 ND/NY/NA 현금 주식, 정수 수량, **지정가만**(스펙의 `trde_tp` 국내 `0` 보통, 미국 `00` 지정가). 국내 가격은 원 단위 정수이면서 KRX 주권 호가단위에 맞아야 하고(ETF 등은 호가단위가 달라 로컬에서 거부될 수 있음), 미국 가격은 센트 단위 1.00달러 이상입니다. 시장가·신용·공매도·환전·정정·취소 주문은 없습니다. 공식 API는 `kt10000`/`kt10001`(`/api/dostk/ordr`), `ust20000`/`ust20001`(`/api/us/ordr`)으로 고정되고, 요청 본문은 검증된 필드로만 만듭니다. 실전 호스트 `https://api.kiwoom.com`만 허용하며 모의 호스트나 `PRD` 환경변수 재정의는 거부합니다.

**`send` 순서**:
1. 티켓 확인: `PREPARED` 상태, 만료 전, halt 없음, 저장된 요청·해시 일치, 해당 시장에 종결되지 않은 주문 없음. BUY는 한도 설정·잔여 한도, SELL은 시범 보유 수량을 확인합니다.
2. 확인 문구(`SEND REAL <side> <market> <symbol> <ticket> <terms_hash 앞 12자>`)를 **사용자가 직접 대화형 터미널에서 정확히 입력**합니다. 파이프·리디렉션 입력은 거부합니다. 터미널을 조작할 수 있는 자동화 도구는 이 문구를 대신 입력할 수 있으므로 `live send`를 에이전트에 맡기지 않습니다. (`auto`의 무인 전송은 이 단계 대신 유효한 arm과 주문 의도(intent)를 확인하며, 나머지 단계는 같습니다.)
3. 조회 전용 클라이언트로 증권사 상태를 새로 조회합니다.
   - BUY 국내: `kt00001`의 `ord_alow_amt`와 `100stk_ord_alow_amt` 중 작은 값(증거금률 100% 기준), `kt00018` 보유잔고.
   - BUY 미국: `ust21110` USD 행의 `fc_entra`·`fc_ord_alowa` 중 작은 값, `ust21070` 보유잔고, `ust21160` 매도환율 `usd_exch_rate`와 `usa20100` 기준환율 `base_exrt`를 교차 확인합니다. 두 환율이 3% 넘게 다르거나 허용 범위(900~2,500원/달러)를 벗어나면 거부하고, 원화 한도 예약에는 더 높은 환율을 적용합니다.
   - SELL 국내: `kt00018`(`qry_tp=1` 합산, KRX)에서 해당 종목 `trde_able_qty`·`rmnd_qty`. SELL 미국: `ust21070`에서 해당 종목 `sell_alowq`·`poss_qty`(`crnc_code=USD`). 행이 중복되거나 형식이 틀리면 거부.
   조회 값 원문은 출력·저장하지 않습니다.
4. BUY: 비용 버퍼(국내 1%, 미국 1.5%) 포함 주문금액 ≤ 실제 주문가능 현금, 원화 예약액 ≤ `floor(원화 주문가능현금 × 현금비율)`, ≤ 주문당 한도, 누적 약정 + 예약액 ≤ 누적 한도. 미국은 증권사 환율로 원화 환산하며 실제 체결 환율·환전 비용을 확정하지 않습니다.
   SELL: 수량 ≤ 시범 보유(아래) 이고 ≤ 증권사 매매가능수량. **증권사 전체 보유수량이 원장 시범 보유수량과 다르면 기존 보유분·외부 매매가 섞인 것으로 보고 거부**합니다. 매매가능수량이 원장 시범 보유보다 적어도 거부합니다.
5. 접근 토큰을 먼저 발급한 뒤 DB 트랜잭션에서 사용한 근거(`attempt_evidence`: 한도 ID·원화 주문가능현금·비율 한도 또는 매매가능수량·원장 수량)를 기록하고 티켓을 `ATTEMPTED`로 표시합니다. SQLite 트리거가 한도·보유수량·중지·다른 미종결 주문을 다시 검사합니다.
6. `KiwoomRealOrderClient.submit`은 원장에서 이 티켓이 방금 `ATTEMPTED`로 기록되었고 근거가 있으며 조건이 해시와 같은지(매도는 SQL 보유 수량까지) 다시 확인한 뒤에만 주문 API를 **정확히 1회** 호출합니다(SDK 인증 재시도도 끔). 원장 확인 없이 직접 호출하면 거부됩니다.
7. `return_code=0`이고 `ord_no`가 비어 있지 않으면 `ACCEPTED`(주문번호·국내 KST/미국 동부 거래일을 내부 식별자로 연결), 그 밖의 모든 경우는 `UNKNOWN`입니다. 기록 전에 프로세스가 죽으면 `ATTEMPTED`로 남고 `UNKNOWN`과 똑같이 취급합니다.

**대사 `reconcile` (읽기 전용) — 국내**: `kt00007` 계좌별주문체결내역상세(`ord_dt`=전송 시각의 KST 날짜, `qry_tp=1`, `stk_bond_tp=1`, `sell_tp` 매수 2/매도 1, 종목, `dmst_stex_tp=KRX`)를 연속조회 키로 끝까지(최대 10페이지) 읽습니다. 주문번호가 일치하는 행이 정확히 1개이고 종목(`A`+코드)·`io_tp_nm`(`현금매수`/`현금매도`)·수량·지정가·거래소·주문시각(전송 −2분~+10분)이 모두 맞아야 합니다.
- `cntr_qty = ord_qty`이고 `ord_remnq = 0` → **FILLED**, 티켓 종결.
- `cntr_qty + ord_remnq = ord_qty` → 접수·미체결 또는 부분체결. 확인된 누적 체결수량만 기록하고 티켓은 계속 시장을 차단합니다.
- 그 밖(거부·취소·만료 등)이나 앱에서 정정·취소한 자식 주문(`ori_ord`)이 있으면 **UNRESOLVED**. 공식 명세에 거부/취소/만료 코드가 열거되어 있지 않아(`acpt_tp`, `mdfy_cncl`은 자유 텍스트) 추정하지 않습니다.
- 체결은 `fills`에 누적 수량 증가분만 추가 기록합니다(같은 값은 무시, 감소하면 불일치로 거부). 잔고 증감으로 체결을 추정하지 않습니다.
- 결과불명(`UNKNOWN`/`ATTEMPTED`) 티켓은 주문번호가 없으므로 사용자가 앱에서 본 번호를 `--order-no`로 입력할 수 있습니다. 그 번호가 조건·시각이 일치하는 **유일한** 주문이고 다른 티켓에 연결되지 않았을 때만 연결합니다. `ATTEMPTED`는 10분이 지나야 합니다.

**대사 — 미국 (`ust21180` 기간별 주문내역, 실제 응답으로 미검증)**: `ust21510`/`ust21150` 대신 사용합니다. 요청은 `strt_dt`~`end_dt`(전송 시각의 KST 날짜와 미국 동부 날짜), `slby_tp`(매수 2/매도 1), `stex_tp`, 종목, `oppo_trde_tp=0`(일반)입니다.
- 목록 키: 명세는 `result_list`, 공식 예시는 `result_lsit`입니다. 각 페이지는 둘 중 **정확히 하나**만 가져야 하고 모든 페이지가 같은 이름을 써야 합니다. 둘 다 있거나 페이지마다 다르면 거부합니다.
- 주문 식별: 주문번호가 같고 주문일이 두 후보 날짜 중 하나인 행이 정확히 1개여야 합니다. 종목·`slby_tp_nm`(`매수`/`매도`, 예시 문구)·`crnc_code=USD`·수량·지정가·`rsrv_tp=일반`·`oppo_trde_tp_nm=일반`이 모두 맞아야 합니다. 주문일·시각의 시간대가 명세에 없어서, KST 해석(KST 날짜, 전송 −2분~+10분)과 미국 동부 해석(동부 날짜, 같은 창) 중 **하나에 맞아야** 합니다. 어느 쪽인지 고르지 않습니다.
- `cntr_qty = ord_qty`, `ord_remnq = 0`, `mdfy_qty = cncl_qty = 0`, `cntr_qty × cntr_uv ≈ cntr_amt`이면 **FILLED**로 종결합니다. 그 밖에는 확인된 누적 `cntr_qty`만 `fills_us`에 기록하고 종결하지 않습니다. 공식 예시 자체가 1주 주문에 체결 0·잔량 0·취소 0을 보여 주므로 잔량 상태를 해석하지 않습니다. 정정·취소 수량이 있으면 UNRESOLVED입니다. 상위 주문번호 필드가 없어 앱에서 넣은 정정·취소 주문은 이 행의 수량으로만 드러납니다.
- 형식이 예상과 다르면(목록 키, 문구, 시간대) 대사는 실패하고 주문은 계속 시장을 막습니다. 추정해서 기록하지 않습니다.

**사람 종결 `close`**: 앱에서 해당 주문에 살아 있는 잔량이 없음(체결 완료·취소·거부·미접수)을 확인한 뒤 `CLOSE REAL <ticket> NO RESEND`를 입력해 종결합니다. 주문을 전송·취소·재전송하지 않습니다. BUY는 확인된 체결만 원장 보유수량에 더하고 예약액은 누적 한도에 계속 포함됩니다. SELL은 주문 수량 전체를 원장 보유수량에서 차감합니다. **확인된 체결이 주문 수량보다 적은 티켓을 사람 종결하면 원장과 실물 잔고가 어긋날 수 있어 해당 시장의 자동매매를 중단합니다.** 다른 시장의 자동 신규 매수도 막고, 매도는 해당 시장의 독립된 안전 조건이 통과할 때만 가능합니다. 현재 이 불확실성을 자동으로 해제하는 절차는 없습니다. 잔량이 실제로 살아 있다면 먼저 앱에서 취소하세요.

**BUY 한도 `cap`**: 시장별로 `누적 약정 한도(원)`, `주문당 한도(원)`, `주문가능현금 대비 비율(%)`을 명시합니다. 설정 전에는 BUY가 차단되며 기본값은 없습니다(과거 시장별 50,000원 배정은 철회되어 집행하지 않고 기록으로만 남습니다). 누적 약정은 v1 레거시 티켓을 포함한 모든 전송 시도 BUY 예약액의 합이며, 매도나 종결로 줄지 않습니다. 한도 변경은 확인 문구가 필요하고 `risk_caps`에 추가만 되며 `status`에 전체 이력이 표시됩니다. 전송 중 한도가 바뀌면 그 전송은 거부됩니다.

**시범 보유(SELL 가능 수량, 국내·미국)** = 이 프로그램의 BUY 주문에서 `kt00007`/`ust21180`으로 확인된 체결수량 합 − 이 종목의 모든 전송 시도 SELL 수량. 기존 보유분·식별되지 않은 주식은 포함되지 않습니다. 같은 계산이 SQLite 트리거에도 있어, 동시 실행·재시작에도 초과 매도 시도를 막습니다. 추가로 전송 직전 증권사 전체 보유수량이 시범 보유수량과 **정확히 같아야** 매도합니다. 외부에서 같은 종목을 수동으로 사고팔아 귀속이 흐려졌다면 해당 종목의 자동 매도를 사용하지 마세요.

**안전 규칙** (코드와 SQLite 트리거 양쪽에서 강제; 행 삭제·수정 불가)
- 티켓 조건·요청은 생성 후 변경할 수 없습니다. 상태는 `PREPARED → ATTEMPTED → ACCEPTED|UNKNOWN` 한 방향입니다. 두 번째 `send`는 전송하지 않습니다. 자동 재전송·백그라운드 작업이 없습니다.
- **ACCEPTED는 체결이 아닙니다.** 출력에는 주문번호 끝 3자리만 보입니다.
- 해당 시장에 종결되지 않은 `ATTEMPTED/ACCEPTED/UNKNOWN` 주문이 있으면 BUY·SELL 모두 거부합니다. 시장별 유효 티켓은 1개만 만들 수 있습니다.
- `halt`는 영구적이며 새 `prepare`/`send`를 막습니다(`reconcile`/`close`는 가능). `resume`은 없습니다. 이미 `ATTEMPTED`인 전송이나 증권사에 도달한 주문을 취소한다고 보장할 수 없으며, 보유 종목을 자동 매도하지 않습니다.
- DB에는 키·토큰·계좌번호를 저장하지 않습니다. 주문번호, 사용한 환율, 예약액, 전송 근거(원화 주문가능현금·매매가능수량), 확인된 체결수량·평균 체결단가를 저장합니다. 오류는 예외 클래스 이름만 기록합니다.

**DB 마이그레이션 (v1 → v2 → v3)**: `live`/`auto` 명령을 처음 실행할 때 한 트랜잭션에서 새 테이블·뷰·트리거를 추가하고 현재 버전의 트리거 본문으로 갱신합니다. 기존 티켓·중지·메타·한도·체결 행은 수정·삭제하지 않습니다. 예상한 테이블 제약·뷰·트리거와 구조가 다르면 롤백하고 DB를 열지 않습니다. 구버전 코드로 다시 열면 교체된 트리거가 생겨 주문이 막힐 수 있으므로 구버전으로 실행하지 마세요.

추가로 `sell_ownership_checks`에 전송 직전 전체 보유수량을 기록합니다. 매도 시 전체 보유수량과 프로그램 보유수량이 같아야 한다는 조건을 v3 주문 게이트에 적용하며, 기존 원장에도 같은 트리거를 트랜잭션 안에서 갱신합니다.

**수동 `live` 티켓에 없는 것**: 거래 캘린더·시세 신선도·손실 기준 검사는 `auto` 실행에만 있습니다. 수동 티켓은 사람이 가격·시간을 판단합니다. 수수료·세금·환전 비용은 버퍼 추정일 뿐이며, 취소·정정 주문 기능은 없습니다.

### 무인 자동 실행: `auto` (실제 돈, 설정·arm 전에는 비활성)

> **코드 기능과 실제 활성화는 다릅니다.** 오프라인 안전 검사와 실계좌 읽기 전용 응답 일부를 확인했습니다. 모델 호출·실제 주문·체결은 실행하지 않았고 자동매매도 설정하거나 arm하지 않았습니다. 장중 호가 신선도와 체결 대사 형식은 미검증입니다. 전략(모델 제안·기준 규칙)은 수익성이 **입증되지 않았습니다**. 자세한 검증 범위는 [VALIDATION.md](VALIDATION.md)를 보세요.

```powershell
.\.venv\Scripts\python.exe -m stocklab auto template > auto-config.json   # 값이 전부 null인 틀. 직접 채움
.\.venv\Scripts\python.exe -m stocklab auto config --file auto-config.json --reason "사유"   # 검증 + 확인 문구
.\.venv\Scripts\python.exe -m stocklab auto run-once --dry-run            # 조회 API만 사용, 판단·가상 주문 기록
.\.venv\Scripts\python.exe -m stocklab auto watch --dry-run --max-cycles 12
.\.venv\Scripts\python.exe -m stocklab auto arm --hours 8 --reason "사유"  # 확인 문구. 이때부터 실제 주문 가능
.\.venv\Scripts\python.exe -m stocklab auto watch                         # 권한이 사라지면 스스로 종료
.\.venv\Scripts\python.exe -m stocklab auto disarm --reason "중지"         # 즉시 권한 철회
.\.venv\Scripts\python.exe -m stocklab auto status                        # 오프라인
```

**설정 (`auto config`)**: 기본값이 없습니다. 필드가 빠지거나 알 수 없는 필드가 있으면 거부하고, 설정이 없거나 무효하면 주문하지 않습니다. 설정은 추가만 되고, 새 설정을 저장하면 기존 arm은 무효가 됩니다.
- `capital_cap_krw`: 총 자본 상한(원). 이 프로그램이 보유한 수량의 원화 매입가 합과 미종결 BUY 예약액의 합이 넘을 수 없습니다. **계좌 현금을 전부 쓰지 않습니다.**
- 시장별 `universe`(최대 8종목, KR `KRX`, US `ND/NY/NA`), `max_order_krw`, `max_position_krw`, `max_daily_loss_krw`, `max_orders_per_day`, `costs`(수수료·매도세·슬리피지, US는 환전 bps), 장 시작 후·마감 전 여유(분, KR 마감 전 ≥ 15분), `calendar`.
- `calendar`: KRX·NYSE/Nasdaq 공식 공지에서 옮겨 적은 거래일·정규장 시각(최대 200일, 출처 필수). 유효 기간 안에 없는 날은 휴장이고, 기간 밖이면 주문하지 않습니다. 요일 규칙으로 추정하지 않습니다. 미국 시각은 법정 서머타임 규칙(3월 둘째 일요일~11월 첫째 일요일, 2007~2030년만 허용)으로 변환하고, 시스템 시간대 DB가 있으면 둘이 일치해야 합니다.
- `max_total_loss_krw`, `cycle`(간격 ≥ 120초, 분봉 수, 분봉·호가 최대 나이, 가격 칼라·최대 스프레드 bps), `proposer`(`MODEL`: `provider`는 `codex_cli`만 허용·정확한 모델 ID(기본값 없음)·`max_calls_per_day`(1~200)·`timeout_seconds`(30~600), 또는 `BASELINE`), `baseline` 임계값, `arm_max_hours`(≤ 168). 이전 `openai`(API 키) 설정, `anthropic` 제공자, `claude*` 모델, `max_output_tokens`·토큰 단가 필드는 거부됩니다. 이전 `openai` 설정이 최신으로 저장되어 있으면 `auto run-once`/`watch`/`status`가 설정 오류로 멈추므로(주문 없음) `auto template`으로 새 설정을 저장하세요. 새 설정은 해시가 달라 기존 arm을 무효로 만듭니다.

**LIVE 모델 제안자 (`MODEL`)**: [live_ai.py](stocklab/live_ai.py)가 설치된 Codex CLI(`codex exec`)를 사용자의 **ChatGPT 로그인(구독)** 으로 한 번 실행합니다. OpenAI API 키·API 과금 경로는 없고, API·다른 제공자·다른 모델로의 대체나 재시도도 없습니다.
- 로그인 확인: 매 호출 전 `codex login status`가 `Logged in using ChatGPT`여야 합니다. API 키 로그인, 미로그인, 확인 시간 초과(20초)는 호출하지 않고 HOLD입니다. 로그인 정보는 읽거나 바꾸지 않습니다.
- 실행: 빈 임시 디렉터리에서 `codex exec`를 읽기 전용·일회성 세션으로 실행합니다. 사용자 설정과 규칙은 건너뛰고 셸 도구와 웹 검색을 끄며, ChatGPT 로그인만 허용하고 `AGENTS.md` 입력 크기를 0으로 제한합니다. 출력은 JSON 스키마로 고정합니다. 프롬프트(지시 + 검증된 시세 전용 스냅샷)는 표준 입력으로만 보내며 계좌·현금·수량·키는 보내지 않습니다.
- 환경변수: 경로·사용자 프로필·`CODEX_HOME`·프록시 등 CLI 실행과 ChatGPT 로그인에 필요한 OS 변수만 넘기고, 키움·OpenAI·Anthropic 키, `CODEX_API_KEY`, `NODE_OPTIONS` 등은 제거합니다.
- 실패 시 HOLD(재시도 없음): CLI 없음, 로그인 실패, 사용량·속도 제한, 시간 초과, 0이 아닌 종료 코드, stdout 1 MB/stderr 64 KB/최종 출력 8 KB 초과, JSONL 이벤트 형식 오류, 도구 활동(명령 실행·파일 변경·MCP·웹 검색 등; 이벤트가 보이는 즉시 프로세스 트리를 종료), 메시지·턴이 정확히 1개가 아님, 출력 파일 없음·이벤트와 불일치·잘못된 JSON·중복 키, `validate_proposal` 실패. CLI의 stderr·원문 출력은 저장·출력하지 않고 고정된 오류 분류만 남깁니다. Windows에서는 창 없이 실행하고 시간 초과 시 `taskkill /T`로 하위 프로세스까지 종료합니다.
- 비용 기록: 구독 호출은 `model_cost_krw`를 0원으로 기록합니다. 이는 호출당 청구가 없다는 뜻일 뿐 무료라는 뜻이 아니며, 구독 사용량 한도가 소진되면 HOLD가 됩니다. 이전 API 호출 행의 비용은 그대로 합산됩니다. `--dry-run`도 `MODEL`이면 실제로 Codex를 실행해 구독 사용량을 씁니다.
- 기록: `model_meta_json`에 제공자(`codex_cli`), 과금 방식(`chatgpt_subscription`), 모델 ID(CLI에 지정한 값), 스레드 ID, CLI가 보고한 사용량(input/cached_input/output/reasoning_output 토큰), 프롬프트·스냅샷 해시, 오류 분류를 남기고 `auto status`에 표시합니다. 모델 출력은 제안일 뿐이며 수량·가격은 위험 엔진이 정합니다.

**arm (`auto arm`)**: 확인 문구 `ARM REAL AUTO <설정 해시 12자> <N>H`를 대화형 터미널에서 입력합니다. arm은 최신 설정과 시장별 최신 `live cap`에 묶이고 N시간 뒤 만료됩니다. 활성 시장에 `live cap`이 없으면 arm 하지 않습니다. 다음 경우 권한이 즉시 사라집니다: 새 설정, 새 `live cap`, `auto disarm`, 만료, `live halt --market ALL`, 손실 트리거, 결과불명 주문, 종결 오류. 같은 검사가 Python과 SQLite 트리거(주문 의도 생성·`ATTEMPTED` 전환) 양쪽에 있고, 전송 직전 `submit`에서 한 번 더 확인합니다. 재시작한 프로세스도 기존 arm이 여전히 유효할 때만 이어서 동작하며 스스로 다시 arm 하지 않습니다.

**한 주기 (시장별, 정규장 창 안에서만)**
1. 주기 키 `시장:세션일:슬롯:모드`를 먼저 기록합니다. 같은 키는 재시작 후에도 다시 실행하지 않습니다. 단일 실행 잠금은 `%LOCALAPPDATA%\StockLab\auto.lock`입니다.
2. 이 시장의 미종결 티켓을 모두 읽기 전용으로 대사합니다. 하나라도 남으면 모델을 부르지 않고 관망합니다. 주문번호가 없는 결과불명 티켓은 사람이 확인해야 하므로 disarm합니다.
3. 증거 스냅샷 `stocklab-live-evidence-v2`를 만듭니다. 대상은 설정 종목과 보유 중인 시범 종목입니다.
   - **현재 정규장 분봉만 사용**: 설정 캘린더의 오늘 세션 `[open_utc, close_utc]`(주기 판단에 쓴 값 그대로) 안의 1분봉만 제안자에게 보냅니다. 시가 이전 분봉(전일 세션·장전)은 버리고 이어 붙이지 않습니다. 같은 세션 분봉이 5개 미만이거나, 마감 이후 분봉이 있거나, 호가 시각이 시가 이전이면 그 시장 전체가 관망합니다. US는 KST/미국 동부 시각 해석을 먼저 확정한 뒤 UTC로 비교하므로 KST 자정을 넘는 미국 세션도 한 세션으로 처리합니다. 세션 경계가 없으면 증거를 만들지 않습니다.
   - 제안자가 받은 모델 스냅샷 전체(가격·거래량·시각·종목·`sellable`만, 최대 64 KB)를 해시와 함께 `evidence_json`에 저장해 판단을 재생할 수 있습니다. 계좌번호·현금·수량·주문번호·키는 들어가지 않습니다. v1 기록(스냅샷 없음)도 그대로 읽힙니다(`auto status`의 `snapshot_replayable=false`). DB 스키마는 바뀌지 않았습니다.
   - KR: `ka10080` 1분봉, `ka10004` 최우선 호가(`bid_req_base_tm`), `ka10100` 종목 상태(`orderWarning=0`, `auditInfo=정상`, 정지·관리 문구 없음).
   - US: `usa06011` 1분봉, `usa20101` 최우선 호가(`dt`+`bid_tm`), `usa20100`(`trd_susp_tp=0`, `curr_unit=USD`, `base_exrt`).
   - 최신 분봉·호가가 설정한 나이를 넘거나, 미래 시각이거나, 순서가 틀리거나, 형식이 어긋나면 그 시장 전체가 관망합니다. US 시각대는 KST와 미국 동부 해석 중 정확히 하나만 최근일 때만 인정하고, 스냅샷 안의 모든 US 시각이 같은 해석이어야 합니다. 모델에는 숫자·시각·증거 ID·`sellable` 여부만 보냅니다. 종목명·뉴스·문자열·계좌번호·잔고·수량·키는 보내지 않습니다. 모의/연구 DB나 합성 가격은 사용하지 않습니다.
4. 증권사 주문가능현금(`live send`와 같은 조회)과 US 환율을 읽고, 기록된 체결로 손익과 노출을 계산해 `auto_marks`에 남깁니다(`live_risk.py` 설명 참조). LIVE와 DRY_RUN의 손익 기록·모델 비용을 분리합니다. 일 손익은 같은 모드의 이전 세션 마지막 기록을 기준으로 계산해 밤사이 가격 변동을 포함하며, 이전 기록이 없다면 0원 또는 첫 기록의 양의 손익을 기준으로 사용합니다. 이전 기록은 장중이거나 며칠 전의 것일 수 있으므로 공식 전일 종가를 뜻하지 않습니다. 시장 일 손실이나 두 시장 합산 누적 손실이 기준에 닿으면 disarm하고 관망합니다. 다른 시장에 보유수량이 있으면 공식 캘린더상 마지막 완료 거래일의 마감 창 근처 기록만 합산합니다. 다른 시장 포지션이 없으면 마지막 확정 손익 기록을 사용합니다. 손익을 계산할 수 없거나 다른 시장 기록이 불확실하면 BUY는 막고 SELL만 허용합니다.
5. 제안: `MODEL`이면 Codex CLI를 ChatGPT 구독 로그인으로 한 번 실행합니다(재시도·API·대체 제공자·대체 모델 없음, 24시간 호출 상한은 LIVE와 DRY_RUN을 합산한 실제 호출 시도 기준). 실패·형식 오류·범위 밖 종목·보유하지 않은 종목의 SELL은 HOLD가 됩니다. 결정론적 기준 규칙(`stocklab-baseline-trend-v1`, 구간 수익률 임계값)의 제안도 항상 함께 기록합니다. 증거·제안·모델 메타데이터·비용은 주문 전에 `auto_decisions`에 변경 불가로 저장됩니다.
   - **연구 전용 후보 `stocklab-research-costaware-drift-v1`** ([live_research.py](stocklab/live_research.py)): 같은 세션 스냅샷과 같은 호가로 매 주기 계산해 `model_meta_json.research_candidate`에 제안·버전·입력 해시·비용 진단을 기록합니다. 규칙: 최근 1분 수익률 10개(연속 1분봉 11개)의 평균·표본표준편차로 t값을 구하고, |t| ≥ 2일 때 평균 × 5분을 단순 외삽한 기대 변화(bps)가 왕복 비용 허들(호가 스프레드 + 매수·매도 수수료 + 매도세 + 슬리피지×2 + US 환전×2, 설정 `costs` 값)을 넘으면 BUY(미보유)/SELL(보유) 후보, 아니면 HOLD입니다. 학습·적합이 없고 외삽값은 보정된 수익 예측이 아닙니다. 이 후보는 위험 엔진(`risk.plan`)에 들어가지 않고 티켓을 만들지 않으며, `proposer.kind`로 선택할 수 없습니다(`MODEL`/`BASELINE`만 허용).
6. 위험 엔진이 주문을 최대 1건 만듭니다. BUY는 최우선 매도호가, SELL은 최우선 매수호가로 냅니다. 스프레드와 최근 체결가 대비 거리가 칼라 안이어야 합니다. BUY 수량은 설정 주문 한도, `live cap` 주문당·잔여 누적 한도, 총 자본 상한 잔여, 종목 한도, 주문가능현금 × 현금 비율 중 최솟값을 비용 버퍼를 포함한 1주 가격으로 나눈 값입니다. US는 환율로 환산하고, 계좌 환율과 시세 환율이 3% 넘게 다르면 거부합니다. SELL은 이 프로그램이 산 확인 수량 전부입니다. 한 종목에는 한 번에 한 포지션만 둡니다.
7. LIVE: 호가를 새로 조회해 다시 계산합니다. 티켓과 주문 의도를 한 트랜잭션에 만들고, `live send`와 같은 경로(증권사 재조회, 게이트, `ATTEMPTED`, 1회 전송 권한 `submission_claims`)로 정확히 1회 전송합니다. 접수가 아니면(UNKNOWN) disarm하고 재전송하지 않습니다. 예상하지 못한 오류는 해당 시장을 영구 halt하고 disarm합니다.

**아직 없는 것·미검증**: 취소·정정 주문, 부분체결 잔량의 자동 종결, 웹소켓 실시간 시세, 공식 장 운영 상태(VI·거래정지) API, 실제 수수료·세금 확인, 성과 평가·보고, 저장된 스냅샷의 일괄 재생·평가 도구, 연구 후보의 과거·전향 성과 검증. 부분체결이나 장 마감 후 남은 주문은 사람이 앱에서 확인하고 `live close`로 종결할 때까지 그 시장을 막습니다. 미확인 잔량이 있는 주문을 사람 종결하면 해당 시장 자동매매가 중단되고 다른 시장 신규 매수도 차단됩니다. 자동 복구 절차는 없습니다. `auto status`의 `unverified` 목록에 있는 필드는 실제 응답으로 확인되지 않았습니다. 로컬 PC 시계가 틀리면 신선도 검사 때문에 모두 관망합니다.

**실사용 전 필요한 단계**: `auto status`의 `steps_before_live`를 참고하세요. 요약하면: 한도(`live cap`) → 설정 작성·저장 → `--dry-run` 관찰 → 실제 응답 형식 확인과 소액 수동 주문·대사 확인(KR·US 각각) → `auto arm` → `auto watch`.

### 공식 클라이언트 설치 / 재현

Python 3.13+, uv가 필요합니다. 기본 연구 도구는 Python 3.11+ 표준 라이브러리만 사용합니다.

```powershell
git clone https://github.com/Kiwoom-Securities/Kiwoom-REST-API.git vendor/kiwoom-official
git -C vendor/kiwoom-official checkout 953e5dbff123f437ab4d11a78a95191a685eb51f
uv venv --python 3.13 .venv
uv pip install --python .venv/Scripts/python.exe ./vendor/kiwoom-official
```

공식 클라이언트는 수정 없이 사용하며 별도 라이선스가 적용됩니다. `vendor/`는 Git에서 제외합니다. 키움 API 이용 목적의 로컬 의존성으로만 사용하며 재배포하지 않습니다. 확인한 공식 리비전: `953e5dbff123f437ab4d11a78a95191a685eb51f`.

## 소액 시범 계획: `pilot`

과거 고정 배분안의 비용과 손실 한도를 시장별로 미리 계산하는 **오프라인 보고서**입니다. 현재 실전 자동매매 한도와 연결되지 않습니다. DB·네트워크·키 저장소·증권사를 사용하지 않고 주문을 보내지 않습니다. 종목 추천이 아닙니다.

실제 수수료·상품별 매도 세율·환율에는 기본값이 없으므로 본인 계좌와 상품 조건을 직접 입력합니다. **계획 손실 기준은 시장별 최대 2,500원, 합계 5,000원**으로 정했습니다. 이 금액은 실제 손실 상한을 보장하지 않습니다. 아래 단가·환율은 형식 예시이며 실제 시세가 아닙니다. 수수료 예시는 키움의 [국내 일반 현금주식 안내](https://www.kiwoom.com/m/domestic/stock/VStockMainView)(매수·매도 각 0.015%, 매도 세금 0.20%)와 [미국주식 온라인 기본수수료 안내](https://download.kiwoom.com/deploy/AG003/pdf/AG003_30.pdf)(매수·매도 각 0.25%)를 사용했습니다. 계좌 우대율과 상품별 세금은 다를 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m stocklab pilot --market KR --budget-krw 50000 --price 10000 --quantity 2 --buy-commission-bps 1.5 --sell-commission-bps 1.5 --sell-tax-bps 20 --slippage-bps 10 --max-loss-krw 2500 --adverse-move-pct 10
.\.venv\Scripts\python.exe -m stocklab pilot --market US --budget-krw 50000 --price 15 --quantity 1 --fx-rate 1400 --fx-cost-bps 10 --buy-commission-bps 25 --sell-commission-bps 25 --sell-tax-bps 0 --slippage-bps 10 --max-loss-krw 2500 --adverse-move-pct 10
```

- bps 단위: 수수료 0.015% = 1.5 bps, 매도 세율 0.20% = 20 bps. 슬리피지는 매수·매도에 각각, 환전 비용은 원→달러·달러→원에 각각 적용합니다. 미국 상품 예시의 세율 0은 다른 규제 수수료까지 0이라는 뜻이 아닙니다.
- 예상 매수 총액(매수 수수료·슬리피지·환전 비용 포함)이 시장별 50,000원 한도를 넘으면 거부합니다. `--budget-krw`는 더 낮출 수만 있습니다. `--max-loss-krw`는 2,500원을 초과할 수 없습니다. 이 명령은 시장 간 합계나 실제 계좌 주문 한도를 집행하지 않습니다.
- 출력: 예상 매수 총액, 남는 현금, 왕복 마찰 비용(원·%), 손익분기 가격 변화율, 지정한 하락 시나리오 1개의 손실과 손실 한도 초과 여부.
- 제외: 입력한 매도 세율 이외의 세금·거래소/규제 수수료, 환율 변동, 최소·정액 수수료, 호가단위, 실제 가격 변동. 환전 비용은 입력 가정으로만 반영합니다. 출력의 `excluded_and_uncertain`을 확인하세요.

구조 결정과 실전 주문 전 필수 조건은 [ARCHITECTURE.md](ARCHITECTURE.md)에 있습니다.

## 세 가지 환경 구분

| 환경 | 명령 | 주문 | 잔고 출처 | 필요한 것 |
|---|---|---|---|---|
| **로컬 모의 시범** (이 문서의 `pilot-*`) | `pilot-setup`, `run --mode paper`, `advance`, `pilot-status` | SQLite 안의 가상 주문·가상 체결만 | 로컬 DB 장부 | 키 불필요. 가격 CSV(또는 합성 데이터) |
| **키움 공식 모의투자 서버** (`mockapi.kiwoom.com`) | `broker ... --mode demo` (인증·조회만) | **이 프로그램은 모의서버 주문을 보내지 않습니다** | 키움 모의계좌 | 사용자가 별도로 발급받은 **모의투자 전용 키** |
| **실전 계좌** (`api.kiwoom.com`) | `broker ... --mode real`, `serve-real` (조회만), `live` (사람 확인 주문), `auto` (설정·arm 후 자동 주문) | 국내·미국 지정가 BUY/SELL 코드. SELL은 이 프로그램에서 확인된 체결 수량만. 대사는 조회만. 실제 주문·체결은 미검증 | 실제 계좌 (계좌 전체) | 실전 키, 명시 한도 및 활성화 절차 |

로컬 모의 시범은 키움 모의서버와 실전 계좌의 잔고를 읽지도, 동기화하지도 않습니다. 키움 모의투자 서버는 국내(KRX)·미국 상장주식을 지원하지만, 모의 키는 실전 키와 별도로 사용자가 포털에서 발급받아야 하며 아직 발급되지 않았을 수 있습니다. 모의서버 주문 경로는 구현되어 있지 않습니다.

키움 모의서버 연결을 준비하려면 [공식 모의투자 이용안내](https://openapi.kiwoom.com/intro/mockInvestInfo?dummyVal=0)에서 **국내·미국 모의투자에 각각 참가 신청**하고 모의투자용 REST App Key·App Secret을 발급받습니다. 키를 채팅에 보내지 말고 이 PC의 로컬 입력창에 넣습니다.

```powershell
.\.venv\Scripts\python.exe -m stocklab broker setup --mode demo
.\.venv\Scripts\python.exe -m stocklab broker check --mode demo --full
```

현재 이 명령은 인증·시세·잔고 **조회만** 확인합니다. 모의서버의 매수·매도·취소·체결 대사는 별도 구현과 모의 키를 통한 확인이 필요합니다.

## 두 시장 로컬 모의 시범: `pilot-setup` / `pilot-status` / `pilot-demo`

국내 50,000원·미국 50,000원 상당 시범을 **로컬 모의 장부**로 운영합니다. 증권사 키·네트워크가 필요 없고 실제 주문이 없습니다. 기존 `init`, `demo`, `pilot`(계획 계산기)은 그대로입니다.

### 1) 합성 연습 (새 DB 전용)

```powershell
.\.venv\Scripts\python.exe -m stocklab --db data/pilot-demo.db pilot-demo
.\.venv\Scripts\python.exe -m stocklab --db data/pilot-demo.db pilot-status
.\.venv\Scripts\python.exe -m stocklab --db data/pilot-demo.db serve
```

`KR_PILOT_*`, `US_PILOT_*`는 **가상 종목·가상 가격·가상 환율(1400원/달러, 10 bps)** 입니다. 모멘텀 기준 전략 주문 → 다음 관측 체결 → **의도적인 국내 합성 하락 구간** → 국내 계획 손실 기준 도달·고정 → 이후 국내 신규 매수 거절, 위험 축소 매도는 허용 → 체결 → 상태 보고 순서로 통제 장치를 보여줍니다. 실제 시장 데이터·백테스트·수익성 근거가 아닙니다.

### 2) 실제 시범 장부 만들기 (새 DB 전용)

```powershell
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db pilot-setup --fx-rate 1400 --fx-cost-bps 10
```

- 위 환율·비용은 형식 예시입니다. 본인이 확인한 원/달러 환율과 환전 비용 가정을 입력합니다. **환율은 조회하지 않으며 시범 전체에 고정**됩니다. 환전 비용만으로 미국 시장 계획 손실 2,500원에 도달하는 설정은 거부합니다.
- 국내 현금 50,000원. 미국 현금 = `floor_to_cent(50,000 / (환율 × (1 + 환전비용)))` 달러. 50,000원 전액을 미국 한도로 계산하고 센트 미만 잔여 원화는 `unconverted_residual_krw`로 기록해 손실에 포함합니다.
- 이미 존재하는 DB 파일(비어 있지 않은 파일)은 열지 않고 거부합니다. 실전 조회용 DB나 기존 데모 DB를 바꾸지 않습니다.
- 선택: `--kr-policy kr.json --us-policy us.json`으로 RiskPolicy(수수료·슬리피지 가정 등)를 지정합니다. `max_order_notional`은 시장 현금을 넘을 수 없고 시세 유효기간은 24시간을 초과할 수 없습니다. 기본 수수료 5 bps·슬리피지 10 bps는 **키움 실제 요율이 아닌 실험 가정**입니다. 실제보다 낮은 비용 가정은 손실을 과소평가합니다.

이후 운영(가격은 시점이 명시된 CSV로 입력):

```powershell
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db import prices my-prices.csv
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db run --market KR --as-of 2026-09-01T06:30:00Z --key kr-2026-09-01 --mode paper --top-k 2
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db run --market US --as-of 2026-09-01T20:00:00Z --key us-2026-09-01 --mode paper --top-k 2
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db advance --market KR --as-of 2026-09-02T06:30:00Z
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db advance --market US --as-of 2026-09-02T20:00:00Z
.\.venv\Scripts\python.exe -m stocklab --db data/pilot.db pilot-status
```

- 기본 전략은 무료 결정론적 기준 전략(`momentum`/`equal`)입니다. 외부 모델 결과는 `--strategy file`, 유료 Claude 추론은 `--strategy anthropic`을 **명시했을 때만** 실행됩니다. 이 과거 연구용 `run` 경로는 실계좌 자동매매(`auto`)의 판단 경로가 **아닙니다**. LIVE 모델 제안자는 OpenAI만 사용합니다.
- **두 시장을 시간 순서대로** 운영합니다(국내 장 마감 06:30Z → 같은 날 미국 20:00Z). 합계 손실 계산은 두 장부를 같은 시점으로 평가하므로, 한 시장의 시계가 앞서 있으면 다른 시장의 신규 매수는 평가 불가로 차단됩니다. 미국 종목을 보유한 경우 금요일 미국 종가가 월요일 국내 장 마감 시점에는 24시간을 넘으므로, 거래 캘린더를 추가하기 전까지 월요일 국내 신규 매수는 차단됩니다.
- `pilot-status --as-of ...`는 지정 시점 이하의 모의 시세만 사용합니다. 계좌 시계 이전 시점은 거부합니다. 시세 누락·만료 시장은 `UNAVAILABLE`로 표시하고 신규 매수를 막으며, 합계 평가가 불가하면 종료 코드 2입니다.

### 손실 기준과 한도 (신규 모의 매수 통제)

- 비용 가정 반영 평가액 = 현금 + Σ(수량 × 최신 모의 시세) × (1 − (수수료+슬리피지) bps). 미국은 × 환율 × (1 − 환전비용)으로 원화 환산합니다. 실제 체결 비용보다 작게 입력하면 이 평가액은 낙관적일 수 있습니다.
- 계획 손실 = 50,000원 − 비용 가정 반영 원화 평가액. `advance`에서 각 시세 관측 시점과 체결 직후, 신규 매수 검사 시 **시장별 2,500원 이상 또는 합계 5,000원 이상**이 관측되면 이 DB에 **고정(해제 없음)** 합니다. 읽기 전용 `pilot-status` 조회만으로는 고정하지 않습니다. 해당 시장(합계면 두 시장)의 미체결 매수는 취소되고 신규 매수는 거절됩니다. 위험 축소 매도는 별도 손실 기준에 막히지 않지만 해당 종목의 유효한 시세가 없거나 계좌가 수동 중지된 경우에는 만들거나 체결할 수 없습니다. **자동 청산은 없습니다.**
- 매수 주문 생성 시와 이후 체결 시마다 검사합니다: 한도(보유 평가액 + 매수금액 + 수수료 ≤ 시장 초기 현금), 매수 후 예상 손실이 기준에 닿는지.
- 이 기준은 **보장된 손절이 아닙니다.** 가격 급변은 기준을 넘어설 수 있습니다. 고정 환율, 기업행사 미반영, 국내 매도세·미국 규제 수수료·최소 수수료·양도세 미반영, 유동성·호가·거래정지 미반영, 주말·휴장 캘린더 없음. `pilot-status` 출력의 `limitations`를 확인하세요.
- 시범 정책이 없는 기존 모의 계좌(`init`으로 만든 DB)는 이전과 똑같이 동작합니다.

## 연구·모의투자 데모

아래 `serve`(포트 8765)는 **로컬 모의 DB를 보여주는 연구용 산출물**이며 실계좌 잔고가 아닙니다. 실제 계좌 확인은 위의 `serve-real`을 사용합니다.

```powershell
python -m stocklab --db data/demo.db demo
python -m stocklab --db data/demo.db serve
```

브라우저: http://127.0.0.1:8765

빈 DB에만 데모가 생성됩니다. KRW·USD 계좌, 합성 가격·뉴스, 주문·부분 체결, 동일비중/모멘텀 비교를 만듭니다. **합성 종목이며 수익성 근거가 아닙니다.** 실제 시장 데이터/백테스트로 오해하지 마세요.

## 과거 신호 오프라인 검증: `historical_eval` (주문 없음)

"과거 신호가 체결됐다면 현재 규칙이 어땠을까"를 **로컬 CSV 분봉만으로** 추정합니다. 증권사·네트워크·유료 모델을 호출하지 않고, 계좌 정보를 읽지 않으며, 주문을 만들지 않습니다.

```powershell
python -m stocklab.historical_eval --input bars.csv --output report.json            # 스프레드 가정 10bp
python -m stocklab.historical_eval --input bars.csv --output report.json --spread-bps 20
python -m stocklab.historical_eval --input bars.csv --output report.json --buy-fee-bps 1.5 --sell-fee-bps 1.5 --sell-tax-bps 20 --slippage-bps 10
```

- 입력 열(정확히 이 순서): `market,symbol,at_utc,open,high,low,close,volume,source`. 한 파일에 한 시장·한 종목, UTC(`+00:00`/`Z`) 분 단위 시각의 엄격한 오름차순·중복 없음, 양수·정합 OHLC, 0 이상 정수 거래량, 기본 `source=kiwoom-real-readonly-minute`(합성·데모 출처는 거부). `source` 문자열은 파일 작성자의 선언이며, 평가기 자체가 증권사 원본 여부를 인증하지는 않습니다.
- 정규장(KR 09:00~15:30 KST, US 09:30~16:00 미 동부, 평일)만 허용합니다. 장외 행이 있으면 거부하며, `--drop-outside-session`을 줄 때만 명시적으로 버리고 개수를 보고합니다. 휴장일·조기폐장 캘린더는 반영하지 않습니다.
- 비교 규칙: `live_ai.baseline`(기본 진입/청산 50bp)과 연구용 `live_research.evaluate_safely`를 **그대로 호출**해 따로 평가합니다. t봉 종가 시점에 t-10..t의 연속 1분 종가 11개만 사용하고 `sellable=false`(매수 신호만)입니다.
- 가상 거래: BUY면 t+1봉 시가 진입, t+5봉 종가 청산. t..t+5가 같은 세션에서 끊김 없이 이어질 때만 채점하며, 거래 중(t+1..t+5) 신호는 건너뜁니다. 빠진 분봉을 채우거나 세션을 넘지 않습니다.
- 비용 가정(bp): 호가는 t봉 종가 중심의 **고정 가정 스프레드**(기본 왕복 10bp, 편도 절반)입니다. KR 매수·매도 수수료 1.5, 매도세 20, 슬리피지 편도 10, 환전 0 / US 수수료 25, 매도세 0, 슬리피지 편도 10, 환전 편도 10. `--buy-fee-bps`, `--sell-fee-bps`, `--sell-tax-bps`, `--slippage-bps`, `--fx-cost-bps`로 확인한 요율을 입력할 수 있습니다. **기본값은 예시이며 실제 수수료율이나 과거 호가를 확인한 값이 아닙니다.**
- 보고서(JSON): 행·세션·적격 구간 수, BUY/HOLD, 거래 수, 총/순 수익 거래 수와 비율, 평균·중앙 순수익(bp), 1단위 순차 복리 수익률과 최대낙폭, 모델 버전과 모든 가정. 거래 0건이면 승률은 `null`(0%가 아님). 거래 30건 미만 또는 거래가 있는 세션 20개 미만이면 `minimum_historical_sample_gate_passed=false`입니다. 이 최소 표본 조건을 통과해도 미래 정확도가 입증되는 것은 아니므로 `future_accuracy_validated`는 항상 `false`입니다.
- **자동매매 엔진의 정확한 재현이 아닙니다.** 매도 경로, 주문·리스크·수량·포트폴리오 시뮬레이션이 없고, 가정된 체결 결과는 미래 성과의 근거가 아닙니다.

## 구현 범위

- SQLite 거래 기록, 주문 키 중복 방지, 주문·잔고·체결 원자적 갱신.
- 데이터 시점·입수 가능 시점 필터, 입력 데이터와 결정 해시 기록.
- 동일비중·모멘텀 기준 전략, 외부 모델 JSON 결과 가져오기, 선택적 Anthropic API 호출.
- 공매도/레버리지 없는 모의 계좌. 종목·전체 비중, 주문 금액, 현금 여유분, 시세 유효기간 검사.
- 후속 가격 관측에서만 체결. 부분 체결·취소·만료·중지 및 재시작 이후 상태 유지.
- 읽기 전용 대시보드 두 종류: `serve-real`(키움 실계좌 전체의 실제 조회값), `serve`(로컬 모의 DB, 연구용). 두 잔고는 별개이며 서로 동기화하지 않습니다.
- 두 시장 로컬 모의 시범: 고정 환율 원화 합산 평가, 시장별 2,500원·합계 5,000원 계획 손실 기준에 따른 신규 매수 차단(고정), 합성 연습 명령.

## 데이터와 시점

가격 CSV 필수 열:

```text
market,symbol,event_at,available_at,price,volume,source,synthetic
US,AAPL,2026-09-01T20:00:00Z,2026-09-01T20:00:01Z,100.00,10000,my-licensed-feed,false
```

위 숫자는 형식 설명용입니다. 실제 AAPL 가격이 아닙니다. `volume`은 해당 관측 구간의 거래량이며 누적 일거래량을 반복 입력하면 안 됩니다.

뉴스 CSV: `market,symbol,published_at,available_at,headline,body,source,synthetic`.

```powershell
python -m stocklab --db data/research.db init --market US --cash 2000
python -m stocklab --db data/research.db import prices prices.csv
python -m stocklab --db data/research.db import news news.csv
python -m stocklab --db data/research.db snapshot --market US --as-of 2026-09-01T20:01:00Z
python -m stocklab --db data/research.db run --market US --as-of 2026-09-01T20:01:00Z --key us-day-1 --mode shadow
```

- `replay`: 원자료의 `available_at`을 신뢰하는 과거 재생. 실제 당시 수집했다는 증거는 아닙니다.
- `recorded`: 이 프로그램에 입력된 `ingested_at`도 판단 시점 이전이어야 합니다.
- `recorded`는 현재 shadow 판단만 지원합니다. 로컬 모의 체결은 `replay` 데이터로 실행합니다.
- 계좌 시계 이전의 잔고는 재구성하지 않습니다. 과거 분석은 새 실험 DB에서 시작합니다.
- 현재 구현은 관측값 기준 시뮬레이터입니다. 시계가 지난 뒤 도착한 과거 시세는 체결에 쓰지 않습니다.
- 보유 종목 시세가 오래되면 모의 주문을 중지합니다. 기본 유효기간은 24시간이며 주말·휴장일 캘린더를 자동 보정하지 않습니다. 일별 종가 연구는 해당 거래일 종가 시점에서 실행합니다.

## 모델 연결 / 기존 프로젝트 재사용

모델은 `targets=[{symbol,weight,reason,evidence_ids}], summary` 형식으로 목표 비중을 제출합니다. 알려진 종목·근거 ID, 숫자, 비중을 별도 코드로 검증합니다.

외부 모델 파일:

```json
{
  "snapshot_hash": "snapshot 명령이 반환한 값",
  "provider": {"model": "실제로 사용한 모델과 버전"},
  "decision": {"targets": [], "summary": "관망"}
}
```

`run --strategy file --decision-file decision.json`으로 가져옵니다. 데이터/계좌 상태가 달라지면 해시 불일치로 거부합니다. 해당 파일 생성 시점과 모델 학습 데이터의 미래 정보 포함 여부는 인증하지 않습니다.

선택적 Claude API(연구용 `run` 전용, 실계좌 `auto` 판단 경로 아님): 로컬 환경변수 `ANTHROPIC_API_KEY`, `STOCKLAB_MODEL` 설정 후 `run --strategy anthropic`을 명시하면 유료 요청 1회를 실행합니다. `.env`는 자동 로드하지 않습니다. 증권사 키는 전송하지 않으며, 해당 연구 스냅샷의 시세·뉴스·모의 포트폴리오가 모델 제공사에 전송됩니다. 최대 입력 100 KB, 출력 2,200 토큰, 자동 재시도 없음.

재사용 설계:

| 구성 | 선택 |
|---|---|
| 브로커 인증·통신 | 키움 공식 SDK를 로컬 의존성으로 사용 |
| 수치 예측 모델 연구 | [Qlib](https://github.com/microsoft/qlib) 결과를 위 JSON 계약에 맞춰 가져올 후보 |
| 뉴스/토론형 AI | [TradingAgents](https://github.com/TauricResearch/TradingAgents) 분석 결과를 동일 계약으로 가져올 후보 |
| 강화학습 | [FinRL](https://github.com/AI4Finance-Foundation/FinRL) 실험은 데이터·비용 기준이 갖춰진 뒤 별도 비교 |
| 위험 검사·중복 주문·실행 기록 | Stock Lab 계층에서 공통 적용 |

Qlib/TradingAgents/FinRL 전용 어댑터나 학습 모델은 아직 설치·검증하지 않았습니다. 기존 프레임워크는 수익 보장 모델이 아니며, 한국 데이터 정규화와 비용 차이는 별도 검증해야 합니다.

## 주문·복구·비용

`run --mode paper`가 로컬 주문을 생성하고 `advance --market US --as-of ...`가 이후 가격으로 모의 체결합니다. 같은 시점 체결은 금지합니다. 새 리밸런싱 전 기존 주문을 체결시키거나 `cancel <order_id>`로 취소해야 합니다.

`halt --market US --reason ...`는 잔여 주문을 취소하며 보유 종목을 자동 매도하지 않습니다. `resume`은 이유를 명시해야 합니다. 중단된 PENDING 실행은 `abandon --key ... --reason ...`으로 실패 처리할 수 있습니다. 기존 키는 재사용하지 않으며 재시도할 경우 새 키가 필요합니다. 중단 시 유료 모델 호출 비용 발생 여부는 제공사에서 확인해야 합니다.

`compare --market US`는 고정된 두 기준 전략의 전체 실험 DB와 보고서를 보존합니다. 파라미터 탐색·통계적 우위·최적 전략 판정은 하지 않습니다. 수수료 기본값 5 bps, 슬리피지 10 bps는 **실험용 가정이며 키움 실제 요율이 아닙니다**. 신규 계좌의 `init --policy policy.json`으로 변경합니다. 매수는 건당 금액 상한으로 수량을 줄이고, 보유 수량을 줄이는 매도는 금액 상한을 면제합니다.

실전 적용 전 남은 작업: 계좌별 API 수수료·환전 조건 반영, 거래 캘린더/호가단위/거래정지/기업행사 처리, 시세 저장·WebSocket 체결 통보, 주문 응답 불명확 시 조회 대사, 실전 한도 및 수동 승인, 복구/운영 검증. 현재 체결 모형은 세금·환전·시장충격·호가 대기열을 포함하지 않습니다.
