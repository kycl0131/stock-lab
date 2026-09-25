"""Separate news/filing collector for the local point-in-time archive (news.py). Never places or touches orders.

    python -m stocklab.news_collect refresh --config news-sources.json --archive data/news/archive.json
    python -m stocklab.news_collect status --archive data/news/archive.json --max-age-seconds 900

Run it on its own schedule (e.g. every few minutes in a separate terminal or task); the trading cycle only reads the
archive and HOLDs when a required feed is missing, failed or older than `proposer.news.max_status_age_seconds`.

Sources (fixed HTTPS hosts and paths; the URL is built from validated parameters only; redirects are refused):
  gdelt      https://api.gdeltproject.org/api/v2/doc/doc  mode=ArtList format=json sort=DateDesc
             query = the configured per-symbol query (mapping evidence), bounded timespan and maxrecords.
             Metadata only (URL, title, seendate). seendate is kept as provider_available_at (GDELT first-seen time,
             not the publisher's time); published_at stays null. At least 5 s between GDELT requests.
  sec_edgar  https://data.sec.gov/submissions/CIK##########.json  for a configured CIK; the response must list the
             symbol in `tickers`. Needs `sec_user_agent` (name and contact e-mail, SEC fair-access policy).
             acceptanceDateTime is kept raw; its time zone is not verified (the value carries "Z" but EDGAR pages
             show Eastern time), so provider_available_at = raw local time + 5 h, the latest possible reading.
  opendart   https://opendart.fss.or.kr/api/list.json  for a configured corp_code when OPENDART_API_KEY is set
             (read from the environment only, never logged, stored or printed). rcept_dt is a date without a time,
             so provider_available_at = the next calendar day 00:00 KST; no intraday filing time is invented.

Fail closed: HTTP/network/timeout errors, redirects, responses over the byte bound, non-JSON, unexpected shapes,
malformed or future provider timestamps, symbol-mapping mismatches and provider error codes make that run FAILED
(no records from it are stored). An individual GDELT article whose link is not a plain public http(s) URL is
rejected and counted. Stored: title, short summary, canonical source link and timestamps; never article bodies.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time as dtime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from . import news
from .domain import ValidationError

CONFIG_SCHEMA = "stocklab-news-sources-v1"
HOSTS = {"gdelt": "api.gdeltproject.org", "sec_edgar": "data.sec.gov", "opendart": "opendart.fss.or.kr"}
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = {"gdelt": 2_000_000, "sec_edgar": 16_000_000, "opendart": 2_000_000}
GDELT_MIN_INTERVAL_S = 5.0
SEC_MIN_INTERVAL_S = 0.5
FUTURE_TOLERANCE = timedelta(minutes=5)
KST = timezone(timedelta(hours=9))
USER_AGENT = "stocklab-news-collector/1 (local research; metadata only)"
_GDELT_QUERY = re.compile(r"[\w \"'().,&:+\-/]{2,200}")
_SEEN = re.compile(r"([0-9]{8})T([0-9]{6})Z")
_SEC_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]{1,6})?Z?")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_SEC_DOC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_UA = re.compile(r"[ -~]{6,200}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class CollectError(news.NewsError):
    """Fixed category; never contains a URL, key, header or provider text."""


# ---------------------------------------------------------------- config

def _int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(f"{name}: integer {low}..{high} required (no default)")
    return value


def validate_config(cfg) -> dict:
    if not isinstance(cfg, dict) or set(cfg) != {"schema", "sec_user_agent", "gdelt", "sec_edgar", "opendart",
                                                 "symbols"} or cfg["schema"] != CONFIG_SCHEMA:
        raise ValidationError(f"config fields must be exactly schema ({CONFIG_SCHEMA}), sec_user_agent, gdelt, "
                              "sec_edgar, opendart, symbols")
    g = cfg["gdelt"]
    if not isinstance(g, dict) or set(g) != {"timespan_hours", "max_records"}:
        raise ValidationError("gdelt fields: timespan_hours, max_records")
    _int(g["timespan_hours"], "gdelt.timespan_hours", 1, 168)
    _int(g["max_records"], "gdelt.max_records", 1, 75)
    s = cfg["sec_edgar"]
    if not isinstance(s, dict) or set(s) != {"lookback_days", "max_records", "forms"}:
        raise ValidationError("sec_edgar fields: lookback_days, max_records, forms (list or null)")
    _int(s["lookback_days"], "sec_edgar.lookback_days", 1, 30)
    _int(s["max_records"], "sec_edgar.max_records", 1, 100)
    if s["forms"] is not None and (not isinstance(s["forms"], list) or not 1 <= len(s["forms"]) <= 40 or any(
            not isinstance(f, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9 /-]{0,19}", f) for f in s["forms"])):
        raise ValidationError("sec_edgar.forms: null or 1..40 form types such as 8-K, 10-Q")
    d = cfg["opendart"]
    if not isinstance(d, dict) or set(d) != {"lookback_days", "max_records"}:
        raise ValidationError("opendart fields: lookback_days, max_records")
    _int(d["lookback_days"], "opendart.lookback_days", 1, 30)
    _int(d["max_records"], "opendart.max_records", 1, 100)
    symbols = cfg["symbols"]
    if not isinstance(symbols, dict) or not symbols or not set(symbols) <= {"KR", "US"}:
        raise ValidationError("symbols: {KR: {...}, US: {...}}")
    needs_sec = False
    allowed = {"KR": {"gdelt_query", "opendart_corp_code"}, "US": {"gdelt_query", "sec_cik"}}
    for market, entries in symbols.items():
        if not isinstance(entries, dict) or not 1 <= len(entries) <= 16:
            raise ValidationError(f"symbols.{market}: 1..16 symbols")
        for symbol, entry in entries.items():
            if not re.fullmatch(news.SYMBOL_PATTERN[market], symbol) or not isinstance(entry, dict) or not entry \
                    or not set(entry) <= allowed[market]:
                raise ValidationError(f"symbols.{market}.{symbol}: fields from {sorted(allowed[market])}")
            if "gdelt_query" in entry and (not isinstance(entry["gdelt_query"], str)
                                           or not _GDELT_QUERY.fullmatch(entry["gdelt_query"])):
                raise ValidationError(f"symbols.{market}.{symbol}.gdelt_query: 2..200 plain characters")
            if "sec_cik" in entry:
                needs_sec = True
                if not isinstance(entry["sec_cik"], str) or not re.fullmatch(r"[0-9]{10}", entry["sec_cik"]):
                    raise ValidationError(f"symbols.US.{symbol}.sec_cik: 10 digits, zero-padded")
            if "opendart_corp_code" in entry and (not isinstance(entry["opendart_corp_code"], str)
                                                  or not re.fullmatch(r"[0-9]{8}", entry["opendart_corp_code"])):
                raise ValidationError(f"symbols.KR.{symbol}.opendart_corp_code: 8 digits")
    ua = cfg["sec_user_agent"]
    if needs_sec and (not isinstance(ua, str) or not _UA.fullmatch(ua) or not _EMAIL.search(ua)):
        raise ValidationError("sec_user_agent: your name and contact e-mail are required for SEC EDGAR requests")
    if ua is not None and (not isinstance(ua, str) or not _UA.fullmatch(ua)):
        raise ValidationError("sec_user_agent: printable ASCII, 6..200 characters, or null")
    return cfg


def jobs(cfg, *, market=None, sources=None) -> list[dict]:
    """(source, market, symbol, mapping) in a fixed order."""
    out = []
    field = {"gdelt": "gdelt_query", "sec_edgar": "sec_cik", "opendart": "opendart_corp_code"}
    for mk in ("KR", "US"):
        if market not in (None, mk):
            continue
        for symbol, entry in sorted(cfg["symbols"].get(mk, {}).items()):
            for source in news.MARKET_SOURCES[mk]:
                if field[source] in entry and (sources is None or source in sources):
                    out.append({"source": source, "market": mk, "symbol": symbol, "mapping": entry[field[source]]})
    return out


# ---------------------------------------------------------------- transport

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None                                  # a 3xx becomes an HTTPError: never follow to another URL


_OPENER = urllib.request.build_opener(_NoRedirect())


def build_url(source: str, path: str, params: dict) -> str:
    if not re.fullmatch(r"(/[A-Za-z0-9_-][A-Za-z0-9._-]*)+", path):
        raise CollectError("URL_NOT_ALLOWED")
    url = f"https://{HOSTS[source]}{path}" + (f"?{urlencode(params)}" if params else "")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOSTS[source] or parts.port is not None or parts.fragment:
        raise CollectError("URL_NOT_ALLOWED")
    return url


def http_get(url: str, headers: dict, *, timeout: int, max_bytes: int) -> bytes:
    """GET on an allow-listed host with a hard timeout and byte bound. Errors become fixed categories only."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise CollectError("URL_NOT_ALLOWED") from None
    if parts.scheme != "https" or parts.hostname not in HOSTS.values() or port is not None or parts.username:
        raise CollectError("URL_NOT_ALLOWED")
    request = urllib.request.Request(url, headers={**headers, "Accept-Encoding": "identity"}, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise CollectError(f"HTTP_{int(response.status)}")
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > max_bytes:
                raise CollectError("RESPONSE_TOO_LARGE")
            raw = response.read(max_bytes + 1)
    except CollectError:
        raise
    except urllib.error.HTTPError as exc:
        raise CollectError(f"HTTP_{int(exc.code) if isinstance(exc.code, int) else 0}") from None
    except TimeoutError:
        raise CollectError("TIMEOUT") from None
    except urllib.error.URLError as exc:
        # urllib wraps socket timeouts as URLError(reason=TimeoutError) on some Windows/Python combinations.
        if isinstance(exc.reason, TimeoutError):
            raise CollectError("TIMEOUT") from None
        raise CollectError("NETWORK") from None
    except (OSError, ValueError):
        raise CollectError("NETWORK") from None
    if len(raw) > max_bytes:
        raise CollectError("RESPONSE_TOO_LARGE")
    return raw


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=news._no_duplicates, parse_constant=news._no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise CollectError("NOT_JSON") from None


# ---------------------------------------------------------------- adapters: (request) and parse(raw) -> records

def gdelt_request(query: str, cfg: dict) -> tuple[str, dict]:
    return build_url("gdelt", "/api/v2/doc/doc", {
        "query": query, "mode": "ArtList", "format": "json", "sort": "DateDesc",
        "maxrecords": str(cfg["gdelt"]["max_records"]), "timespan": f"{cfg['gdelt']['timespan_hours']}h"}), \
        {"User-Agent": USER_AGENT}


def parse_gdelt(raw: bytes, *, market, symbol, query, retrieved_at, run_id, max_records) -> tuple[list, int]:
    body = _json(raw)
    if not isinstance(body, dict):
        raise CollectError("SCHEMA")
    articles = body.get("articles", [])               # GDELT answers {} when nothing matches
    if not isinstance(articles, list) or len(articles) > max_records:
        raise CollectError("SCHEMA")
    records, rejected = [], 0
    for article in articles:
        if not isinstance(article, dict) or not isinstance(article.get("url"), str) \
                or not isinstance(article.get("title"), str) or not isinstance(article.get("seendate"), str):
            raise CollectError("SCHEMA")
        match = _SEEN.fullmatch(article["seendate"])
        if not match:
            raise CollectError("TIMESTAMP")
        try:
            seen = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            raise CollectError("TIMESTAMP") from None
        if seen > retrieved_at + FUTURE_TOLERANCE:
            raise CollectError("FUTURE_TIMESTAMP")
        title = news.clean_text(article["title"], news.MAX_TITLE, unescape=True)
        try:
            url = news.canonical_url(article["url"])
        except news.NewsError:
            rejected += 1                              # unsupported link: never stored, counted in the run
            continue
        if not title:
            rejected += 1
            continue
        records.append(news.make_record(
            source="gdelt", market=market, symbol=symbol, mapping_value=query, item_key=url, url=url, title=title,
            summary="", published_at=None, provider_time_raw=article["seendate"], provider_available_at=seen,
            retrieved_at=retrieved_at, run_id=run_id))
    return records, rejected


def sec_request(cik: str, cfg: dict) -> tuple[str, dict]:
    return build_url("sec_edgar", f"/submissions/CIK{cik}.json", {}), \
        {"User-Agent": cfg["sec_user_agent"], "Accept": "application/json"}


def parse_sec(raw: bytes, *, symbol, cik, retrieved_at, run_id, lookback_days, max_records, forms) -> tuple[list, int]:
    body = _json(raw)
    if not isinstance(body, dict) or not isinstance(body.get("filings"), dict) \
            or not isinstance(body["filings"].get("recent"), dict) or not isinstance(body.get("tickers"), list):
        raise CollectError("SCHEMA")
    if str(body.get("cik", "")).lstrip("0") != cik.lstrip("0") or symbol not in body["tickers"]:
        raise CollectError("MAPPING_MISMATCH")
    recent = body["filings"]["recent"]
    columns = ("accessionNumber", "acceptanceDateTime", "form", "primaryDocument", "primaryDocDescription", "items")
    if any(not isinstance(recent.get(c), list) for c in columns) \
            or len({len(recent[c]) for c in columns}) != 1 or len(recent["accessionNumber"]) > 5000:
        raise CollectError("SCHEMA")
    earliest = retrieved_at - timedelta(days=lookback_days)
    records = []
    for i in range(len(recent["accessionNumber"])):
        accession, accepted, form = recent["accessionNumber"][i], recent["acceptanceDateTime"][i], recent["form"][i]
        if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession) or not isinstance(form, str) \
                or not isinstance(accepted, str):
            raise CollectError("SCHEMA")
        match = _SEC_TIME.fullmatch(accepted)
        if not match:
            raise CollectError("TIMESTAMP")
        try:
            local = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
        except ValueError:
            raise CollectError("TIMESTAMP") from None
        earliest_reading = local.replace(tzinfo=timezone.utc)          # if the "Z" is literally UTC
        latest_reading = earliest_reading + timedelta(hours=5)          # if it is US Eastern (EST, UTC-5)
        if earliest_reading > retrieved_at + FUTURE_TOLERANCE:
            raise CollectError("FUTURE_TIMESTAMP")
        if latest_reading < earliest or (forms is not None and form not in forms):
            continue
        description, items = recent["primaryDocDescription"][i], recent["items"][i]
        document = recent["primaryDocument"][i]
        folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
        url = folder + (document if isinstance(document, str) and _SEC_DOC.fullmatch(document) else "")
        title = news.clean_text(f"{form}: {description}" if isinstance(description, str) and description
                                else form, news.MAX_TITLE)
        summary = news.clean_text(f"items {items}" if isinstance(items, str) and items else "", news.MAX_SUMMARY)
        records.append((latest_reading, news.make_record(
            source="sec_edgar", market="US", symbol=symbol, mapping_value=cik, item_key=accession,
            url=news.canonical_url(url, news.LINK_HOSTS["sec_edgar"]), title=title or form, summary=summary,
            published_at=None, provider_time_raw=accepted, provider_available_at=latest_reading,
            retrieved_at=retrieved_at, run_id=run_id)))
    records.sort(key=lambda p: (p[0], p[1]["id"]), reverse=True)
    return [r for _, r in records[:max_records]], 0


def opendart_key(environ=None) -> str:
    key = (os.environ if environ is None else environ).get("OPENDART_API_KEY", "")
    if not re.fullmatch(r"[0-9A-Za-z]{40}", key):
        raise CollectError("OPENDART_API_KEY_MISSING_OR_INVALID")
    return key


def opendart_request(corp_code: str, cfg: dict, key: str, retrieved_at: datetime) -> tuple[str, dict]:
    today = retrieved_at.astimezone(KST).date()
    begin = today - timedelta(days=cfg["opendart"]["lookback_days"])
    return build_url("opendart", "/api/list.json", {
        "crtfc_key": key, "corp_code": corp_code, "bgn_de": begin.strftime("%Y%m%d"),
        "end_de": today.strftime("%Y%m%d"), "sort": "date", "sort_mth": "desc", "page_no": "1",
        "page_count": str(cfg["opendart"]["max_records"])}), {"User-Agent": USER_AGENT}


def parse_opendart(raw: bytes, *, symbol, corp_code, retrieved_at, run_id, max_records) -> tuple[list, int]:
    body = _json(raw)
    if not isinstance(body, dict) or not isinstance(body.get("status"), str) \
            or not re.fullmatch(r"[0-9]{3}", body["status"]):
        raise CollectError("SCHEMA")
    if body["status"] == "013":                       # OpenDART: no matching data (a valid empty result)
        return [], 0
    if body["status"] != "000":
        raise CollectError(f"API_STATUS_{body['status']}")
    rows = body.get("list")
    if not isinstance(rows, list) or len(rows) > max_records:
        raise CollectError("SCHEMA")
    today = retrieved_at.astimezone(KST).date()
    records = []
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in
                                            ("rcept_no", "rcept_dt", "corp_code", "report_nm", "flr_nm")):
            raise CollectError("SCHEMA")
        if row["corp_code"] != corp_code or row.get("stock_code") not in (symbol, "", None):
            raise CollectError("MAPPING_MISMATCH")
        if not re.fullmatch(r"[0-9]{14}", row["rcept_no"]) or not re.fullmatch(r"[0-9]{8}", row["rcept_dt"]) \
                or row["rcept_no"][:8] != row["rcept_dt"]:
            raise CollectError("TIMESTAMP")
        try:
            filed = date(int(row["rcept_dt"][:4]), int(row["rcept_dt"][4:6]), int(row["rcept_dt"][6:]))
        except ValueError:
            raise CollectError("TIMESTAMP") from None
        if filed > today:
            raise CollectError("FUTURE_TIMESTAMP")
        # Date-only: usable from the next KST calendar day. No intraday time is assumed.
        available = datetime.combine(filed + timedelta(days=1), dtime(0, 0), KST).astimezone(timezone.utc)
        remark = row.get("rm") if isinstance(row.get("rm"), str) else ""
        records.append(news.make_record(
            source="opendart", market="KR", symbol=symbol, mapping_value=corp_code, item_key=row["rcept_no"],
            url=news.canonical_url(f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']}",
                                   news.LINK_HOSTS["opendart"]),
            title=news.clean_text(row["report_nm"], news.MAX_TITLE) or "공시",
            summary=news.clean_text(f"제출인 {row['flr_nm']}" + (f" / 비고 {remark}" if remark else ""),
                                    news.MAX_SUMMARY),
            published_at=None, provider_time_raw=row["rcept_dt"], provider_available_at=available,
            retrieved_at=retrieved_at, run_id=run_id))
    return records, 0


# ---------------------------------------------------------------- one run / refresh

def _utcnow():
    return datetime.now(timezone.utc)


def collect_one(job: dict, cfg: dict, *, get=http_get, clock=_utcnow, environ=None) -> tuple[dict, list]:
    """One source request for one symbol -> (run, records). Never raises for source problems: FAILED run."""
    run_id = secrets.token_hex(8)
    started = clock()
    source, market, symbol, mapping = job["source"], job["market"], job["symbol"], job["mapping"]
    records, rejected, fetched, error = [], 0, 0, None
    try:
        if source == "gdelt":
            url, headers = gdelt_request(mapping, cfg)
        elif source == "sec_edgar":
            url, headers = sec_request(mapping, cfg)
        else:
            url, headers = opendart_request(mapping, cfg, opendart_key(environ), started)
        raw = get(url, headers, timeout=TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES[source])
        retrieved = clock()
        if source == "gdelt":
            records, rejected = parse_gdelt(raw, market=market, symbol=symbol, query=mapping, retrieved_at=retrieved,
                                            run_id=run_id, max_records=cfg["gdelt"]["max_records"])
        elif source == "sec_edgar":
            records, rejected = parse_sec(raw, symbol=symbol, cik=mapping, retrieved_at=retrieved, run_id=run_id,
                                          lookback_days=cfg["sec_edgar"]["lookback_days"],
                                          max_records=cfg["sec_edgar"]["max_records"], forms=cfg["sec_edgar"]["forms"])
        else:
            records, rejected = parse_opendart(raw, symbol=symbol, corp_code=mapping, retrieved_at=retrieved,
                                               run_id=run_id, max_records=cfg["opendart"]["max_records"])
        fetched = len(records) + rejected
    except news.NewsError as exc:
        records, error = [], _category(exc)
    except Exception as exc:  # defensive: an adapter bug is a FAILED run, never a partial archive
        records, error = [], "UNEXPECTED_" + re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__).upper()[:40]
    finished = clock()
    run = {"run_id": run_id, "source": source, "market": market, "symbol": symbol,
           "mapping": {"method": news.MAPPING_METHOD[source], "value": mapping},
           "started_at": news.utc_text(started), "finished_at": news.utc_text(max(finished, started), ceil=True),
           "committed_at": news.utc_text(max(finished, started), ceil=True),
           "status": "FAILED" if error else "OK", "error": error, "fetched": fetched, "added": 0,
           "duplicates": 0, "rejected": rejected}
    return run, records


def _category(exc) -> str:
    text = re.sub(r"[^A-Z0-9_:]", "_", str(exc).split(" ")[0].upper())[:80]
    return text or "FAILED"


def refresh(cfg: dict, archive_path, *, market=None, sources=None, get=http_get, clock=_utcnow,
            sleep=time.sleep, environ=None) -> dict:
    """Collect every configured job once and append the runs/records atomically (one lock, one replace)."""
    cfg = validate_config(cfg)
    with news.archive_lock(archive_path):
        if Path(archive_path).exists():
            archive, _ = news.read_archive(archive_path)      # an invalid archive is never overwritten
        else:
            archive = news.empty_archive()
        runs, last = [], {}
        for job in jobs(cfg, market=market, sources=sources):
            gap = {"gdelt": GDELT_MIN_INTERVAL_S, "sec_edgar": SEC_MIN_INTERVAL_S}.get(job["source"], 0)
            if job["source"] in last:
                wait = gap - (time.monotonic() - last[job["source"]])
                if wait > 0:
                    sleep(wait)
            run, records = collect_one(job, cfg, get=get, clock=clock, environ=environ)
            last[job["source"]] = time.monotonic()
            archive, run = news.merge(archive, run, records)
            runs.append(run)
        if runs:
            # A collector run cannot be used historically before its refreshed archive was atomically published.
            commit_at = max(clock(), max(news.parse_utc(r["finished_at"]) for r in runs)) + timedelta(seconds=1)
            committed_at = news.utc_text(commit_at, ceil=True)
            run_ids = {r["run_id"] for r in runs}
            runs = [{**r, "committed_at": committed_at} for r in runs]
            archive["runs"] = [{**r, "committed_at": committed_at} if r["run_id"] in run_ids else r
                               for r in archive["runs"]]
        sha = news.write_archive(archive, archive_path)
    return {"archive_sha256": sha, "records": len(archive["records"]), "runs": [
        {k: r[k] for k in ("source", "market", "symbol", "status", "error", "fetched", "added", "duplicates",
                           "rejected", "finished_at", "committed_at")} for r in runs]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stocklab.news_collect",
                                     description="Collect news/filing metadata into a local point-in-time archive.")
    sub = parser.add_subparsers(dest="command", required=True)
    rf = sub.add_parser("refresh", help="fetch every configured source once and append to the archive")
    rf.add_argument("--config", required=True, help=f"JSON config ({CONFIG_SCHEMA})")
    rf.add_argument("--archive", required=True, help="archive JSON under git-ignored data/ or artifacts/")
    rf.add_argument("--market", choices=("KR", "US"), default=None)
    rf.add_argument("--source", action="append", choices=news.SOURCES, default=None)
    st = sub.add_parser("status", help="offline: per-feed freshness and counts")
    st.add_argument("--archive", required=True)
    st.add_argument("--max-age-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            try:
                cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                raise ValidationError("config is unreadable or not JSON") from None
            result = refresh(cfg, args.archive, market=args.market, sources=args.source)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if all(r["status"] == "OK" for r in result["runs"]) else 3
        if not 60 <= args.max_age_seconds <= 86400:
            raise ValidationError("--max-age-seconds must be 60..86400")
        news.check_private_output(args.archive)
        archive, sha = news.read_archive(args.archive)
        print(json.dumps({**news.status(archive, now=_utcnow(), max_age_s=args.max_age_seconds),
                          "archive_sha256": sha}, ensure_ascii=False, indent=2))
        return 0
    except ValidationError as exc:
        print(f"news_collect: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:        # archive write/lock problems; the message is a local path error, never a secret
        print(f"news_collect: {type(exc).__name__}: archive could not be written", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
