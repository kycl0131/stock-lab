"""Point-in-time news and filing evidence: strict record schema, local archive, freshness and as-of selection.

No network here (news_collect.py fetches). Everything is metadata only: title, short summary, source link and
timestamps; article bodies are never fetched or stored.

Times on every record (all UTC ISO-8601, seconds):
  published_at           provider publication time if the provider states one with a time of day, else null
  provider_available_at  the provider's own availability / first-seen time (GDELT seendate, SEC acceptance time
                         read conservatively, OpenDART next KST calendar day 00:00 because rcept_dt is date-only)
  retrieved_at           when OUR collector received the response (local clock, set by the collector only)
  available_at           max(provider_available_at, retrieved_at, published_at): the earliest instant the
                         collector received the record. Point-in-time selection also requires the owning run's
                         committed_at, when the completed archive became available to the trading process.

Every collector attempt appends a run row per (source, market, symbol): OK with counts, or FAILED with a fixed
error category. A decision uses news only when, for every candidate and every required source, the newest run that
finished by the decision time is OK and recent enough; otherwise the caller HOLDs without a model call. An OK run
with zero matching records is valid coverage (an empty item list), which is different from a failed or stale feed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import live_ai
from .domain import ValidationError, digest
from .ts_forecast import check_private_output

ARCHIVE_SCHEMA = "stocklab-news-archive-v2"
RECORD_SCHEMA = "stocklab-news-record-v1"
SOURCES = live_ai.NEWS_SOURCES                      # ("gdelt", "sec_edgar", "opendart")
MARKET_SOURCES = {"KR": ("gdelt", "opendart"), "US": ("gdelt", "sec_edgar")}
MAPPING_METHOD = {"gdelt": "gdelt_query", "sec_edgar": "sec_cik", "opendart": "opendart_corp_code"}
TIME_PRECISION = {"gdelt": "PROVIDER_SEEN_SECOND", "sec_edgar": "ACCEPTANCE_CONSERVATIVE_PLUS_5H",
                  "opendart": "DATE_ONLY_NEXT_DAY_KST"}
LINK_HOSTS = {"sec_edgar": ("www.sec.gov",), "opendart": ("dart.fss.or.kr",)}   # links we construct ourselves
SYMBOL_PATTERN = {"KR": r"[0-9]{6}", "US": r"[A-Z]{1,5}"}
MAX_ARCHIVE_BYTES = 64_000_000
MAX_RECORDS = 200_000
MAX_RUNS = 200_000
MAX_URL, MAX_TITLE, MAX_SUMMARY, MAX_RAW_TEXT = 600, 300, 600, 4000
CLOCK_SKEW = timedelta(seconds=60)
GDELT_TITLE_DEDUPE_WINDOW = timedelta(days=7)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[0-9a-f]{16}")
_ERROR = re.compile(r"[A-Z0-9_:]{1,80}")
_UNSAFE_CHARS = live_ai.NEWS_TEXT_UNSAFE
_HOST = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_TRACKING = re.compile(r"(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ocid|cmpid)", re.IGNORECASE)
_RECORD_FIELDS = {"schema", "id", "source", "market", "symbol", "mapping", "item_key", "url", "title", "summary",
                  "published_at", "provider_time_raw", "time_precision", "provider_available_at", "retrieved_at",
                  "available_at", "content_hash", "run_id"}
_RUN_FIELDS = {"run_id", "source", "market", "symbol", "mapping", "started_at", "finished_at", "committed_at",
               "status", "error",
               "fetched", "added", "duplicates", "rejected"}


class NewsError(ValidationError):
    """Fixed-category failure; the caller HOLDs (live) or reports no coverage (historical)."""


# ---------------------------------------------------------------- primitives

def utc_text(value: datetime, *, ceil: bool = False) -> str:
    """Canonical UTC text with whole seconds. `ceil` rounds up, so a local observation time is never earlier than
    the instant it describes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise NewsError("NAIVE_TIMESTAMP")
    if ceil and value.microsecond:
        value += timedelta(microseconds=1_000_000 - value.microsecond)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc(value, what="TIMESTAMP") -> datetime:
    """Strict canonical form written by utc_text (UTC, whole seconds); anything else fails closed."""
    if not isinstance(value, str) or len(value) != 25:
        raise NewsError(f"{what}_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise NewsError(f"{what}_INVALID") from None
    if parsed.utcoffset() != timedelta(0) or utc_text(parsed) != value:
        raise NewsError(f"{what}_INVALID")
    return parsed


def clean_text(value, limit: int, *, unescape: bool = False) -> str:
    """Untrusted provider text -> one NFC line without control/format characters, bounded (idempotent without
    `unescape`, which collectors use once for HTML-escaped provider titles). Oversize raw input and non-strings fail
    closed rather than being silently cut from an unknown length."""
    if not isinstance(value, str) or len(value) > MAX_RAW_TEXT:
        raise NewsError("TEXT_INVALID_OR_OVERSIZE")
    text = unicodedata.normalize("NFC", html.unescape(value) if unescape else value)
    text = " ".join(_UNSAFE_CHARS.sub(" ", text).split())
    return text[:limit].strip()


def canonical_url(value, allowed_hosts=None) -> str:
    """http(s) link with a public DNS host, no credentials/port/fragment/tracking parameters. It is stored as a
    citation only and never fetched. `allowed_hosts` restricts links we construct (SEC, DART)."""
    if not isinstance(value, str) or not 10 <= len(value) <= MAX_URL or _UNSAFE_CHARS.search(value) or " " in value:
        raise NewsError("URL_INVALID")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise NewsError("URL_INVALID") from None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password or port is not None:
        raise NewsError("URL_INVALID")
    try:
        ipaddress.ip_address(host.strip("[]"))
        literal_ip = True
    except ValueError:
        literal_ip = False
    if literal_ip or not _HOST.fullmatch(host) or host.endswith((".local", ".localhost", ".internal", ".lan")):
        raise NewsError("URL_HOST_UNSUPPORTED")
    if allowed_hosts is not None and (host not in allowed_hosts or parts.scheme.lower() != "https"):
        raise NewsError("URL_HOST_UNSUPPORTED")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.fullmatch(k)])
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", query, ""))


def record_id(source, market, symbol, item_key) -> str:
    return digest([RECORD_SCHEMA, source, market, symbol, item_key])


def content_hash(record: dict) -> str:
    return digest({k: record[k] for k in ("url", "title", "summary", "published_at", "provider_available_at")})


def make_record(*, source, market, symbol, mapping_value, item_key, url, title, summary, published_at,
                provider_time_raw, provider_available_at: datetime, retrieved_at: datetime, run_id) -> dict:
    published = utc_text(published_at, ceil=True) if published_at is not None else None
    available = max([provider_available_at, retrieved_at] + ([published_at] if published_at is not None else []))
    record = {"schema": RECORD_SCHEMA, "id": record_id(source, market, symbol, item_key), "source": source,
              "market": market, "symbol": symbol,
              "mapping": {"method": MAPPING_METHOD[source], "value": mapping_value},
              "item_key": item_key, "url": url, "title": title, "summary": summary, "published_at": published,
              "provider_time_raw": provider_time_raw, "time_precision": TIME_PRECISION[source],
              "provider_available_at": utc_text(provider_available_at, ceil=True),
              "retrieved_at": utc_text(retrieved_at, ceil=True), "available_at": utc_text(available, ceil=True),
              "run_id": run_id}
    record["content_hash"] = content_hash(record)
    return validate_record(record)


# ---------------------------------------------------------------- validation

def _exact(value, fields, what):
    if not isinstance(value, dict) or set(value) != fields:
        raise NewsError(f"{what}_FIELDS")
    return value


def _market_symbol(market, symbol):
    if market not in SYMBOL_PATTERN or not isinstance(symbol, str) or not re.fullmatch(SYMBOL_PATTERN[market], symbol):
        raise NewsError("MARKET_OR_SYMBOL_INVALID")


def _mapping(mapping, source):
    _exact(mapping, {"method", "value"}, "MAPPING")
    value = mapping["value"]
    patterns = {"gdelt": None, "sec_edgar": r"[0-9]{10}", "opendart": r"[0-9]{8}"}
    if mapping["method"] != MAPPING_METHOD[source] or not isinstance(value, str) or not 1 <= len(value) <= 200 \
            or (patterns[source] and not re.fullmatch(patterns[source], value)) or _UNSAFE_CHARS.search(value):
        raise NewsError("MAPPING_INVALID")


def validate_record(record) -> dict:
    _exact(record, _RECORD_FIELDS, "RECORD")
    if record["schema"] != RECORD_SCHEMA or record["source"] not in SOURCES:
        raise NewsError("RECORD_SCHEMA_OR_SOURCE")
    _market_symbol(record["market"], record["symbol"])
    if record["source"] not in MARKET_SOURCES[record["market"]]:
        raise NewsError("RECORD_SOURCE_NOT_FOR_MARKET")
    _mapping(record["mapping"], record["source"])
    if not isinstance(record["item_key"], str) or not 1 <= len(record["item_key"]) <= MAX_URL:
        raise NewsError("RECORD_ITEM_KEY_INVALID")
    if record["id"] != record_id(record["source"], record["market"], record["symbol"], record["item_key"]):
        raise NewsError("RECORD_ID_MISMATCH")
    if canonical_url(record["url"], LINK_HOSTS.get(record["source"])) != record["url"]:
        raise NewsError("RECORD_URL_NOT_CANONICAL")
    for key, limit, empty in (("title", MAX_TITLE, False), ("summary", MAX_SUMMARY, True)):
        value = record[key]
        if not isinstance(value, str) or clean_text(value, limit) != value or (not value and not empty):
            raise NewsError("RECORD_TEXT_INVALID")
    if not isinstance(record["provider_time_raw"], str) or not 1 <= len(record["provider_time_raw"]) <= 40 \
            or not re.fullmatch(r"[0-9A-Za-z:.+\- ]+", record["provider_time_raw"]):
        raise NewsError("RECORD_RAW_TIME_INVALID")
    if record["time_precision"] != TIME_PRECISION[record["source"]]:
        raise NewsError("RECORD_TIME_PRECISION_INVALID")
    stamps = [parse_utc(record["provider_available_at"]), parse_utc(record["retrieved_at"])]
    if record["published_at"] is not None:
        stamps.append(parse_utc(record["published_at"]))
    if parse_utc(record["available_at"]) != max(stamps):
        raise NewsError("RECORD_AVAILABLE_AT_NOT_MAX")
    if not isinstance(record["run_id"], str) or not _RUN_ID.fullmatch(record["run_id"]):
        raise NewsError("RECORD_RUN_ID_INVALID")
    if record["content_hash"] != content_hash(record):
        raise NewsError("RECORD_CONTENT_HASH_MISMATCH")
    return record


def validate_run(run) -> dict:
    _exact(run, _RUN_FIELDS, "RUN")
    if not isinstance(run["run_id"], str) or not _RUN_ID.fullmatch(run["run_id"]) or run["source"] not in SOURCES:
        raise NewsError("RUN_ID_OR_SOURCE_INVALID")
    _market_symbol(run["market"], run["symbol"])
    if run["source"] not in MARKET_SOURCES[run["market"]]:
        raise NewsError("RUN_SOURCE_NOT_FOR_MARKET")
    _mapping(run["mapping"], run["source"])
    if not parse_utc(run["started_at"]) <= parse_utc(run["finished_at"]) <= parse_utc(run["committed_at"]):
        raise NewsError("RUN_TIMES_INVALID")
    if run["status"] == "OK":
        if run["error"] is not None:
            raise NewsError("RUN_STATUS_INVALID")
    elif run["status"] != "FAILED" or not isinstance(run["error"], str) or not _ERROR.fullmatch(run["error"]):
        raise NewsError("RUN_STATUS_INVALID")
    for key in ("fetched", "added", "duplicates", "rejected"):
        if isinstance(run[key], bool) or not isinstance(run[key], int) or not 0 <= run[key] <= 100_000:
            raise NewsError("RUN_COUNT_INVALID")
    return run


def validate_archive(archive) -> dict:
    _exact(archive, {"schema", "records", "runs"}, "ARCHIVE")
    if archive["schema"] != ARCHIVE_SCHEMA:
        raise NewsError("ARCHIVE_SCHEMA")
    records, runs = archive["records"], archive["runs"]
    if not isinstance(records, list) or not isinstance(runs, list) or len(records) > MAX_RECORDS \
            or len(runs) > MAX_RUNS:
        raise NewsError("ARCHIVE_SIZE")
    run_ids = set()
    runs_by_id = {}
    for run in runs:
        validate_run(run)
        if run["run_id"] in run_ids:
            raise NewsError("ARCHIVE_DUPLICATE_RUN")
        run_ids.add(run["run_id"])
        runs_by_id[run["run_id"]] = run
    ids = set()
    for record in records:
        validate_record(record)
        if record["id"] in ids:
            raise NewsError("ARCHIVE_DUPLICATE_RECORD")
        if record["run_id"] not in run_ids:
            raise NewsError("ARCHIVE_RECORD_WITHOUT_RUN")
        owner = runs_by_id[record["run_id"]]
        retrieved = parse_utc(record["retrieved_at"])
        if not parse_utc(owner["started_at"]) <= retrieved <= parse_utc(owner["finished_at"]):
            raise NewsError("ARCHIVE_RECORD_RUN_TIME_MISMATCH")
        ids.add(record["id"])
    return archive


# ---------------------------------------------------------------- archive IO

def empty_archive() -> dict:
    return {"schema": ARCHIVE_SCHEMA, "records": [], "runs": []}


def _no_duplicates(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise NewsError("ARCHIVE_DUPLICATE_KEY")
    return dict(pairs)


def _no_constant(_name):
    raise NewsError("ARCHIVE_NON_FINITE_NUMBER")


def read_archive(path) -> tuple[dict, str]:
    """(validated archive, SHA-256 of the file bytes). Missing, oversize, non-JSON or invalid -> NewsError."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ARCHIVE_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise NewsError("ARCHIVE_UNREADABLE") from None
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise NewsError("ARCHIVE_TOO_LARGE")
    try:
        archive = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NewsError("ARCHIVE_NOT_JSON") from None
    return validate_archive(archive), hashlib.sha256(raw).hexdigest()


def write_archive(archive: dict, path) -> str:
    """Atomic replace: validated JSON to a temporary file in the same directory, fsync, os.replace. A crash leaves
    either the old or the new archive, never a partial one. Returns the SHA-256 of the written bytes."""
    target = check_private_output(path)
    raw = (json.dumps(validate_archive(archive), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise NewsError("ARCHIVE_FULL_START_A_NEW_FILE")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".tmp-{os.getpid()}")
    try:
        with open(temp, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        if temp.exists():
            temp.unlink()
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def archive_lock(path):
    """One collector per archive: an exclusive lock file created with O_EXCL next to the archive."""
    target = check_private_output(path)
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise NewsError("ARCHIVE_LOCKED_BY_ANOTHER_COLLECTOR (remove the .lock file only if no collector runs)") \
            from None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def title_key(record: dict) -> str:
    return digest([record["market"], record["symbol"], record["source"],
                   " ".join(re.sub(r"[^\w]+", " ", record["title"].casefold()).split())])


def merge(archive: dict, run: dict, records: list) -> tuple[dict, dict]:
    """Append one run and its new records. The first observation of an item is kept unchanged (its retrieved_at
    and available_at are never moved); refetched items and GDELT same-title syndications count as duplicates."""
    validate_run(run)
    existing_ids = {r["id"] for r in archive["records"]}
    existing_titles = {}
    for existing in archive["records"]:
        if existing["source"] == "gdelt":
            existing_titles.setdefault(title_key(existing), []).append(parse_utc(existing["available_at"]))
    added, duplicates = [], 0
    for record in records:
        validate_record(record)
        if record["run_id"] != run["run_id"] or (record["source"], record["market"], record["symbol"]) \
                != (run["source"], run["market"], run["symbol"]):
            raise NewsError("RECORD_RUN_MISMATCH")
        key = title_key(record) if record["source"] == "gdelt" else None
        observed = parse_utc(record["available_at"])
        same_title_recently = key is not None and any(
            abs((observed - prior).total_seconds()) <= GDELT_TITLE_DEDUPE_WINDOW.total_seconds()
            for prior in existing_titles.get(key, ()))
        if record["id"] in existing_ids or same_title_recently:
            duplicates += 1
            continue
        existing_ids.add(record["id"])
        if key is not None:
            existing_titles.setdefault(key, []).append(observed)
        added.append(record)
    run = {**run, "added": len(added), "duplicates": duplicates}
    merged = {"schema": ARCHIVE_SCHEMA,
              "records": sorted(archive["records"] + added, key=lambda r: (r["available_at"], r["id"])),
              "runs": archive["runs"] + [validate_run(run)]}
    if len(merged["records"]) > MAX_RECORDS or len(merged["runs"]) > MAX_RUNS:
        raise NewsError("ARCHIVE_FULL_START_A_NEW_FILE")
    return merged, run


# ---------------------------------------------------------------- freshness and point-in-time selection

def feed_state(archive: dict, *, source, market, symbol, at: datetime, max_age_s: int | None) -> dict:
    """The newest archive-visible run for the key must be OK and (unless max_age_s is None) at most max_age_s old.
    `committed_at`, not request completion, is when the live process could first read the run."""
    at_text = utc_text(at)
    runs = [r for r in archive["runs"] if r["committed_at"] <= at_text
            and (r["source"], r["market"], r["symbol"]) == (source, market, symbol)]
    if not runs:
        raise NewsError(f"{symbol}:{source}:NO_COLLECTOR_RUN")
    latest = max(runs, key=lambda r: (r["committed_at"], r["run_id"]))
    if latest["status"] != "OK":
        raise NewsError(f"{symbol}:{source}:SOURCE_FAILED:{latest['error']}")
    if max_age_s is not None and (at - parse_utc(latest["committed_at"])).total_seconds() > max_age_s:
        raise NewsError(f"{symbol}:{source}:FEED_STALE")
    return latest


def evidence_object(archive: dict, *, market: str, candidates, sources, as_of: datetime, as_of_text: str,
                    max_age_s: int, lookback_s: int, max_items_per_symbol: int,
                    shift: timedelta = timedelta(0)) -> tuple[dict, dict]:
    """live_ai news object for `candidates` [(symbol, label)] as of `as_of`, or NewsError when any required feed is
    missing, failed or stale. Items: available_at <= as_of (strict point in time) and within the lookback window,
    same-title duplicates collapsed to the earliest, newest first, bounded per symbol, in total and in bytes.
    `label`/`shift` anonymise the symbol and move every timestamp by the same amount (historical tests)."""
    if not 1 <= max_items_per_symbol <= live_ai.NEWS_MAX_ITEMS_PER_SYMBOL:
        raise NewsError("MAX_ITEMS_PER_SYMBOL_INVALID")
    coverage, chosen, runs_used = [], [], {}
    first_text, as_of_floor = utc_text(as_of - timedelta(seconds=lookback_s), ceil=True), utc_text(as_of)
    for symbol, label in candidates:
        for source in sources:
            run = feed_state(archive, source=source, market=market, symbol=symbol, at=as_of, max_age_s=max_age_s)
            runs_used[f"{symbol}:{source}"] = run["run_id"]
        coverage.append({"symbol": label, "source": source,
                         "collected_at": utc_text(parse_utc(run["committed_at"]) - shift)})
        pool, titles = [], set()
        runs_by_id = {r["run_id"]: r for r in archive["runs"]}
        candidates = []
        for record in archive["records"]:
            if (record["market"], record["symbol"]) != (market, symbol) or record["source"] not in sources:
                continue
            owner = runs_by_id[record["run_id"]]
            available = max(parse_utc(record["available_at"]), parse_utc(owner["committed_at"]))
            if first_text <= utc_text(available) <= as_of_floor:
                candidates.append((available, record))
        for available, record in sorted(candidates, key=lambda p: (p[0], p[1]["id"])):
            key = title_key(record)
            if key in titles:
                continue
            titles.add(key)
            pool.append((available, record, label))
        chosen += sorted(pool, key=lambda p: (p[0], p[1]["id"]), reverse=True)[:max_items_per_symbol]
    chosen.sort(key=lambda p: (p[0], p[1]["id"]), reverse=True)
    chosen = chosen[:live_ai.NEWS_MAX_ITEMS]
    while True:
        items = [{"id": "N-" + record["id"][:16], "symbol": label, "source": record["source"],
                  "available_at": utc_text(available - shift),
                  "title": clean_text(record["title"], live_ai.NEWS_MAX_TITLE),
                  "summary": clean_text(record["summary"], live_ai.NEWS_MAX_SUMMARY)}
                 for available, record, label in chosen]
        news = {"schema": live_ai.NEWS_SCHEMA, "as_of": as_of_text, "coverage": coverage, "items": items}
        if len(json.dumps(news, ensure_ascii=False).encode("utf-8")) <= live_ai.NEWS_MAX_BYTES or not chosen:
            break
        chosen = chosen[:-1]                        # drop the oldest item until the object fits its byte budget
    audit = {"runs": runs_used, "record_ids": [record["id"] for _, record, _ in chosen],
             "items": len(chosen)}
    return news, audit


# ---------------------------------------------------------------- LIVE config and evidence

def validate_config(nc, enabled_universe: dict) -> dict:
    """`proposer.news`: archive path, freshness, lookback, item bound and required sources per enabled market."""
    if not isinstance(nc, dict) or set(nc) != {"archive_path", "max_status_age_seconds", "lookback_hours",
                                               "max_items_per_symbol", "required_sources"}:
        raise ValidationError("proposer.news 필드는 정확히 archive_path, max_status_age_seconds, lookback_hours, "
                              "max_items_per_symbol, required_sources 입니다.")
    path = nc["archive_path"]
    if not isinstance(path, str) or not 1 <= len(path) <= 400 or not Path(path).is_absolute():
        raise ValidationError("proposer.news.archive_path는 절대 경로여야 합니다.")
    for key, low, high in (("max_status_age_seconds", 60, 3600), ("lookback_hours", 1, 168),
                           ("max_items_per_symbol", 1, live_ai.NEWS_MAX_ITEMS_PER_SYMBOL)):
        value = nc[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValidationError(f"proposer.news.{key}: {low}~{high} 사이 정수여야 합니다 (기본값 없음).")
    required = nc["required_sources"]
    if not isinstance(required, dict) or not set(required) <= {"KR", "US"} or set(enabled_universe) - set(required):
        raise ValidationError("proposer.news.required_sources에 활성 시장마다 출처 목록이 필요합니다.")
    for market, sources in required.items():
        if not isinstance(sources, list) or not sources or len(set(sources)) != len(sources) \
                or not set(sources) <= set(MARKET_SOURCES[market]):
            raise ValidationError(f"proposer.news.required_sources.{market}: {MARKET_SOURCES[market]} 중 1개 이상.")
    return nc


def live_evidence(nc: dict, snapshot: dict, *, now: datetime) -> tuple[dict, dict]:
    """News object for a validated LIVE snapshot. Raises NewsError (-> HOLD, no model call) when the archive is
    unreadable/invalid, any required feed is missing, failed or stale at as_of, a newer run by `now` failed, or a
    run claims to have finished in the future."""
    safe = live_ai.validate_snapshot(snapshot)
    market = safe["market"]
    as_of = live_ai._timestamp(safe["as_of"]).astimezone(timezone.utc)
    if as_of > now + CLOCK_SKEW:
        raise NewsError("SNAPSHOT_AFTER_NOW")
    archive, archive_sha = read_archive(nc["archive_path"])
    latest_allowed = utc_text(now + CLOCK_SKEW)
    if any(r["committed_at"] > latest_allowed for r in archive["runs"]):
        raise NewsError("COLLECTOR_RUN_IN_FUTURE")
    sources = nc["required_sources"].get(market)
    if not sources:
        raise NewsError("NO_REQUIRED_SOURCES_FOR_MARKET")
    symbols = [c["symbol"] for c in safe["candidates"]]
    for symbol in symbols:                          # a failure after as_of but before now also blocks the call
        for source in sources:
            feed_state(archive, source=source, market=market, symbol=symbol, at=now + CLOCK_SKEW, max_age_s=None)
    news, audit = evidence_object(archive, market=market, candidates=[(s, s) for s in symbols], sources=sources,
                                  as_of=as_of, as_of_text=safe["as_of"], max_age_s=nc["max_status_age_seconds"],
                                  lookback_s=nc["lookback_hours"] * 3600,
                                  max_items_per_symbol=nc["max_items_per_symbol"])
    live_ai.validate_news(news, safe)
    return news, {**audit, "archive_sha256": archive_sha, "archive_path_hash": digest(str(nc["archive_path"]))}


# ---------------------------------------------------------------- status (offline)

def status(archive: dict, *, now: datetime, max_age_s: int) -> dict:
    keys = sorted({(r["source"], r["market"], r["symbol"]) for r in archive["runs"]})
    feeds = []
    for key in keys:
        source, market, symbol = key
        runs = [r for r in archive["runs"] if (r["source"], r["market"], r["symbol"]) == key]
        latest = max(runs, key=lambda r: (r["committed_at"], r["run_id"]))
        try:
            feed_state(archive, source=source, market=market, symbol=symbol, at=now, max_age_s=max_age_s)
            state = "FRESH"
        except NewsError as exc:
            state = str(exc).split(":", 2)[-1]
        feeds.append({"source": source, "market": market, "symbol": symbol, "state": state,
                      "latest_status": latest["status"], "latest_error": latest["error"],
                      "latest_finished_at": latest["finished_at"], "latest_committed_at": latest["committed_at"],
                      "last_ok_at": max((r["committed_at"] for r in runs if r["status"] == "OK"), default=None),
                      "runs": len(runs),
                      "records": sum(1 for r in archive["records"] if (r["source"], r["market"], r["symbol"]) == key)})
    first = min((r["retrieved_at"] for r in archive["records"]), default=None)
    return {"schema": archive["schema"], "records": len(archive["records"]), "runs": len(archive["runs"]),
            "earliest_retrieved_at": first,
            "point_in_time_note": "records are usable only after both available_at and the owning run committed_at; "
                                  "decisions before the archive commit have no news coverage",
            "feeds": feeds}
