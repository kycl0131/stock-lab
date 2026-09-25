"""Versioned ARX/ridge time-series forecaster for one market/symbol. Dependency-free; places no orders.

    python -m stocklab.ts_forecast train --input bars.csv --output artifacts/ts-005930.json \
        --train-end-session 2026-09-18

Model `stocklab-ts-arx-ridge-v1` (fixed before any holdout is looked at; nothing is tuned on holdout data):
  window    WINDOW_BARS = 31 consecutive one-minute bars t-30..t of one regular session (30 one-minute returns).
            A missing, repeated or gapped minute, or a session boundary inside the window, rejects the window.
  features  (all from bars t-30..t only)
              ret_1m_bps, ret_5m_bps, ret_10m_bps, ret_30m_bps   log(close_t / close_{t-k}) x 10000
              realized_vol_30m_bps                              sqrt(sum of the 30 squared 1-minute log returns)
              log_rel_volume_5_30                               log((mean volume t-4..t + 1) / (mean volume t-30..t + 1))
  target    (close_{t+30} / close_t - 1) x 10000, gross of costs; bars t..t+30 must be contiguous in one session.
  fit       features standardised with training means / population std; ridge with an unpenalised intercept,
            penalty RIDGE_LAMBDA_PER_ROW x rows on the standardised coefficients (a fixed, predeclared value).
  training  only sessions <= --train-end-session (explicit, no default). Later sessions in the file are a
            chronological holdout: reported as a diagnostic, never used to fit, scale or choose anything.
  eligible  at least MIN_TRAIN_SESSIONS sessions and MIN_TRAIN_ROWS rows, finite values, no constant feature.

Rows overlap (one per eligible minute), so they are strongly autocorrelated; the residual std is an in-sample
diagnostic, not a calibrated uncertainty. Nothing here claims predictive power or profitability.

The artifact is immutable JSON (never overwritten) carrying its own content hash. Loading requires the pinned
SHA-256 of the file bytes, the exact schema, matching market/symbol and finite numbers; anything else fails closed.
"""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from . import historical_eval as he, live_ai, live_research
from .domain import ValidationError, digest

MODEL_VERSION = "stocklab-ts-arx-ridge-v1"
ARTIFACT_SCHEMA = "stocklab-ts-artifact-v1"
WINDOW_BARS = 31
HORIZON_MINUTES = live_ai.FORECAST_HORIZON_MINUTES   # 30
BAR_SECONDS = 60
FEATURES = ("ret_1m_bps", "ret_5m_bps", "ret_10m_bps", "ret_30m_bps", "realized_vol_30m_bps", "log_rel_volume_5_30")
TARGET = "(close[t+30] / close[t] - 1) * 10000, gross of costs"
RIDGE_LAMBDA_PER_ROW = 0.1       # predeclared; not searched
MIN_TRAIN_SESSIONS = 5
MIN_TRAIN_ROWS = 500
MAX_ABS_FORECAST_BPS = 1000.0    # a larger prediction is treated as a model failure (HOLD)
MAX_ARTIFACT_BYTES = 200_000
NOTICE = ("Linear ridge forecast of the 30-minute gross return from the previous 30 minutes of one symbol. "
          "Research artifact: not validated out of sample, not a calibrated probability, no profitability claim.")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REPO = Path(__file__).resolve().parents[1]


class ForecastError(ValidationError):
    pass


class TrainingError(ValidationError):
    pass


# ---------------------------------------------------------------- features / target

def features(closes, volumes) -> list[float]:
    """Feature vector for the decision bar (the last element); uses exactly WINDOW_BARS bars and nothing later."""
    if len(closes) != WINDOW_BARS or len(volumes) != WINDOW_BARS:
        raise ForecastError("WINDOW_SIZE")
    c = [float(x) for x in closes]
    v = [float(x) for x in volumes]
    if not all(math.isfinite(x) and x > 0 for x in c) or not all(math.isfinite(x) and x >= 0 for x in v):
        raise ForecastError("WINDOW_VALUES_INVALID")
    last = WINDOW_BARS - 1
    ret = lambda k: math.log(c[last] / c[last - k]) * 10000  # noqa: E731
    one_minute = [math.log(c[i] / c[i - 1]) * 10000 for i in range(1, WINDOW_BARS)]
    x = [ret(1), ret(5), ret(10), ret(30), math.sqrt(math.fsum(r * r for r in one_minute)),
         math.log((math.fsum(v[-5:]) / 5 + 1) / (math.fsum(v) / WINDOW_BARS + 1))]
    if not all(math.isfinite(value) for value in x):
        raise ForecastError("FEATURE_NOT_FINITE")
    return x


def target_bps(close_t, close_exit) -> float:
    return (float(close_exit) / float(close_t) - 1) * 10000


def link_counts(bars) -> tuple[list[int], list[int]]:
    """back[t]: contiguous one-minute same-session links ending at t; ahead[t]: links starting at t."""
    n = len(bars)
    back, ahead = [0] * n, [0] * n
    for t in range(1, n):
        back[t] = back[t - 1] + 1 if he._linked(bars, t - 1) else 0
    for t in range(n - 2, -1, -1):
        ahead[t] = ahead[t + 1] + 1 if he._linked(bars, t) else 0
    return back, ahead


def window_features(bars, t) -> list[float]:
    window = bars[t - WINDOW_BARS + 1:t + 1]
    return features([b["close"] for b in window], [b["volume"] for b in window])


def samples(bars, sessions) -> list[dict]:
    """(t, x, y) for every bar t of `sessions` with a full contiguous window and a full contiguous target path."""
    back, ahead = link_counts(bars)
    out = []
    for t, bar in enumerate(bars):
        if bar["session"] in sessions and back[t] >= WINDOW_BARS - 1 and ahead[t] >= HORIZON_MINUTES:
            out.append({"t": t, "x": window_features(bars, t),
                        "y": target_bps(bar["close"], bars[t + HORIZON_MINUTES]["close"])})
    return out


# ---------------------------------------------------------------- ridge

def _solve(a, b):
    """Gaussian elimination with partial pivoting (a is small and symmetric positive definite)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise TrainingError("RIDGE_SYSTEM_SINGULAR")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for k in range(col, n + 1):
                m[r][k] -= f * m[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - math.fsum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def fit(xs, ys) -> dict:
    n, k = len(xs), len(FEATURES)
    means = [math.fsum(x[j] for x in xs) / n for j in range(k)]
    scales = [math.sqrt(math.fsum((x[j] - means[j]) ** 2 for x in xs) / n) for j in range(k)]
    if not all(math.isfinite(s) and s > 1e-9 for s in scales):
        raise TrainingError("CONSTANT_OR_INVALID_FEATURE")
    z = [[(x[j] - means[j]) / scales[j] for j in range(k)] for x in xs]
    intercept = math.fsum(ys) / n
    yc = [y - intercept for y in ys]
    penalty = RIDGE_LAMBDA_PER_ROW * n
    a = [[math.fsum(row[i] * row[j] for row in z) + (penalty if i == j else 0.0) for j in range(k)] for i in range(k)]
    b = [math.fsum(row[i] * yv for row, yv in zip(z, yc)) for i in range(k)]
    coefficients = _solve(a, b)
    residuals = [y - _linear(intercept, coefficients, means, scales, x) for x, y in zip(xs, ys)]
    std = math.sqrt(math.fsum(r * r for r in residuals) / (n - k - 1))
    model = {"intercept": intercept, "coefficients": coefficients, "means": means, "scales": scales,
             "residual_std_bps": std, "residual_mean_abs_bps": math.fsum(abs(r) for r in residuals) / n}
    if not all(math.isfinite(v) for v in [intercept, std, *coefficients, *means]) or std <= 0:
        raise TrainingError("FIT_NOT_FINITE")
    return model


def _linear(intercept, coefficients, means, scales, x) -> float:
    return intercept + math.fsum(c * (v - m) / s for c, v, m, s in zip(coefficients, x, means, scales))


def diagnostics(predicted, realized) -> dict:
    n = len(predicted)
    if not n:
        return {"rows": 0}
    errors = [p - r for p, r in zip(predicted, realized)]
    hits = sum((p > 0) == (r > 0) for p, r in zip(predicted, realized) if r != 0)
    nonzero = sum(r != 0 for r in realized)
    return {"rows": n, "mae_bps": round(math.fsum(abs(e) for e in errors) / n, 4),
            "rmse_bps": round(math.sqrt(math.fsum(e * e for e in errors) / n), 4),
            "direction_hit_rate": round(hits / nonzero, 4) if nonzero else None}


# ---------------------------------------------------------------- artifact

def _session_list(bars) -> list[str]:
    return sorted({b["session"] for b in bars})


def train(data: dict, train_sessions) -> dict:
    """Artifact dict from `data` (historical_eval.load_bars) using only `train_sessions`."""
    chosen = set(train_sessions)
    if len(chosen) < MIN_TRAIN_SESSIONS:
        raise TrainingError(f"fewer than {MIN_TRAIN_SESSIONS} training sessions")
    rows = samples(data["bars"], chosen)
    if len(rows) < MIN_TRAIN_ROWS:
        raise TrainingError(f"fewer than {MIN_TRAIN_ROWS} eligible training rows ({len(rows)})")
    used = sorted({data["bars"][r["t"]]["session"] for r in rows})
    if len(used) < MIN_TRAIN_SESSIONS:
        raise TrainingError(f"fewer than {MIN_TRAIN_SESSIONS} sessions with eligible rows")
    model = fit([r["x"] for r in rows], [r["y"] for r in rows])
    train_bars = [[b["at"].isoformat(), f"{b['open']:f}", f"{b['high']:f}", f"{b['low']:f}", f"{b['close']:f}",
                   b["volume"]] for b in data["bars"] if b["session"] in chosen]
    body = {"schema": ARTIFACT_SCHEMA, "model_version": MODEL_VERSION, "market": data["market"],
            "symbol": data["symbol"], "source": data["source"], "window_bars": WINDOW_BARS,
            "horizon_minutes": HORIZON_MINUTES, "bar_seconds": BAR_SECONDS, "features": list(FEATURES),
            "target": TARGET, "ridge_lambda_per_row": RIDGE_LAMBDA_PER_ROW,
            "train": {"first_session": used[0], "last_session": used[-1], "sessions": len(used), "rows": len(rows)},
            "data": {"input_sha256": data["sha256"], "train_bars_sha256": digest(train_bars)},
            "scaler": {"means": model["means"], "scales": model["scales"]},
            "intercept": model["intercept"], "coefficients": model["coefficients"],
            "residual": {"std_bps": model["residual_std_bps"], "mean_abs_bps": model["residual_mean_abs_bps"]},
            "notice": NOTICE}
    return {**body, "content_sha256": digest(body)}


def predict(artifact: dict, x) -> float:
    value = _linear(artifact["intercept"], artifact["coefficients"], artifact["scaler"]["means"],
                    artifact["scaler"]["scales"], x)
    if not math.isfinite(value) or abs(value) > MAX_ABS_FORECAST_BPS:
        raise ForecastError("FORECAST_OUT_OF_RANGE")
    return value


def artifact_bytes(artifact: dict) -> bytes:
    return (json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def check_private_output(path) -> Path:
    """Artifacts and reports stay out of the public tree: outside the repo, or under git-ignored artifacts/ or data/."""
    resolved = Path(path).resolve()
    if _REPO in resolved.parents and not any(_REPO / d in resolved.parents for d in ("artifacts", "data")):
        raise ValidationError("write model artifacts/reports under artifacts/ or data/ (git-ignored) "
                              "or outside the repository")
    return resolved


def save_artifact(artifact: dict, path) -> str:
    """Write once (refuses to overwrite). Returns the SHA-256 of the file bytes (the value to pin in config)."""
    raw = artifact_bytes(validate_artifact(json.loads(artifact_bytes(artifact))))
    with open(check_private_output(path), "xb") as handle:
        handle.write(raw)
    return hashlib.sha256(raw).hexdigest()


def _no_duplicates(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise ForecastError("ARTIFACT_DUPLICATE_KEY")
    return dict(pairs)


def _no_constant(_name):
    raise ForecastError("ARTIFACT_NON_FINITE_NUMBER")


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ForecastError("ARTIFACT_NUMBER_INVALID")
    return float(value)


def _count(value, low, high) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ForecastError("ARTIFACT_COUNT_INVALID")
    return value


def _exact(value, keys, what):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ForecastError(f"ARTIFACT_{what}_FIELDS")
    return value


def validate_artifact(artifact) -> dict:
    """Strict structure, constants of this model version, finite numbers and the self content hash."""
    _exact(artifact, ("schema", "model_version", "market", "symbol", "source", "window_bars", "horizon_minutes",
                      "bar_seconds", "features", "target", "ridge_lambda_per_row", "train", "data", "scaler",
                      "intercept", "coefficients", "residual", "notice", "content_sha256"), "ROOT")
    if (artifact["schema"], artifact["model_version"], artifact["window_bars"], artifact["horizon_minutes"],
            artifact["bar_seconds"], artifact["features"], artifact["target"], artifact["ridge_lambda_per_row"],
            artifact["notice"]) != (ARTIFACT_SCHEMA, MODEL_VERSION, WINDOW_BARS, HORIZON_MINUTES, BAR_SECONDS,
                                    list(FEATURES), TARGET, RIDGE_LAMBDA_PER_ROW, NOTICE):
        raise ForecastError("ARTIFACT_VERSION_OR_SPEC_MISMATCH")
    market, symbol = artifact["market"], artifact["symbol"]
    if not isinstance(market, str) or market not in he.SYMBOL_PATTERN or not isinstance(symbol, str) \
            or not re.fullmatch(he.SYMBOL_PATTERN[market], symbol):
        raise ForecastError("ARTIFACT_MARKET_OR_SYMBOL_INVALID")
    if not isinstance(artifact["source"], str) or not 1 <= len(artifact["source"]) <= 80 \
            or he.SYNTHETIC_MARKERS.search(artifact["source"]):
        raise ForecastError("ARTIFACT_SOURCE_INVALID")
    train_info = _exact(artifact["train"], ("first_session", "last_session", "sessions", "rows"), "TRAIN")
    for key in ("first_session", "last_session"):
        if not isinstance(train_info[key], str) or not _SESSION.fullmatch(train_info[key]):
            raise ForecastError("ARTIFACT_SESSION_INVALID")
        try:
            date.fromisoformat(train_info[key])
        except ValueError:
            raise ForecastError("ARTIFACT_SESSION_INVALID") from None
    if train_info["first_session"] > train_info["last_session"]:
        raise ForecastError("ARTIFACT_SESSION_RANGE_INVALID")
    _count(train_info["sessions"], MIN_TRAIN_SESSIONS, 100_000)
    _count(train_info["rows"], MIN_TRAIN_ROWS, 100_000_000)
    hashes = _exact(artifact["data"], ("input_sha256", "train_bars_sha256"), "DATA")
    if not all(isinstance(v, str) and _SHA256.fullmatch(v) for v in hashes.values()):
        raise ForecastError("ARTIFACT_DATA_HASH_INVALID")
    scaler = _exact(artifact["scaler"], ("means", "scales"), "SCALER")
    k = len(FEATURES)
    for values in (scaler["means"], scaler["scales"], artifact["coefficients"]):
        if not isinstance(values, list) or len(values) != k:
            raise ForecastError("ARTIFACT_VECTOR_LENGTH")
        for value in values:
            _number(value)
    if any(s <= 0 for s in scaler["scales"]):
        raise ForecastError("ARTIFACT_SCALE_NOT_POSITIVE")
    _number(artifact["intercept"])
    residual = _exact(artifact["residual"], ("std_bps", "mean_abs_bps"), "RESIDUAL")
    if _number(residual["std_bps"]) <= 0 or _number(residual["mean_abs_bps"]) < 0:
        raise ForecastError("ARTIFACT_RESIDUAL_INVALID")
    body = {key: value for key, value in artifact.items() if key != "content_sha256"}
    if artifact["content_sha256"] != digest(body):
        raise ForecastError("ARTIFACT_CONTENT_HASH_MISMATCH")
    return artifact


def load_artifact(path, *, expected_sha256: str, market: str, symbol: str) -> dict:
    """Read, verify the pinned file hash, parse strictly and validate. Raises ForecastError."""
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise ForecastError("ARTIFACT_PIN_INVALID")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise ForecastError("ARTIFACT_UNREADABLE") from None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ForecastError("ARTIFACT_TOO_LARGE")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ForecastError("ARTIFACT_SHA256_MISMATCH")
    try:
        artifact = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ForecastError("ARTIFACT_NOT_JSON") from None
    artifact = validate_artifact(artifact)
    if (artifact["market"], artifact["symbol"]) != (market, symbol):
        raise ForecastError("ARTIFACT_MARKET_SYMBOL_MISMATCH")
    return artifact


# ---------------------------------------------------------------- forecast object (the Codex input)

def _bps(value) -> str:
    return f"{Decimal(repr(float(value))).quantize(Decimal('0.01'))}"


def forecast_entry(artifact: dict, symbol: str, gross_bps: float, roundtrip_cost_bps) -> dict:
    """One validated-shape forecast entry. `symbol` may be an anonymised label (historical tests)."""
    gross = _bps(gross_bps)
    cost = f"{Decimal(roundtrip_cost_bps).quantize(Decimal('0.01'))}"
    return {"symbol": symbol, "model_version": artifact["model_version"], "predicted_gross_bps": gross,
            "residual_std_bps": _bps(artifact["residual"]["std_bps"]), "train_sessions": artifact["train"]["sessions"],
            "train_rows": artifact["train"]["rows"], "roundtrip_cost_bps": cost,
            "predicted_net_bps": f"{Decimal(gross) - Decimal(cost)}"}


def forecast_object(entries) -> dict:
    return {"schema": live_ai.FORECAST_SCHEMA, "horizon_minutes": HORIZON_MINUTES, "forecasts": list(entries)}


def snapshot_window(candidate: dict, as_of: str, max_bar_age_s: int) -> tuple[list, list]:
    """Closes/volumes of the newest WINDOW_BARS observations; they must be exactly one minute apart and fresh."""
    observations = candidate["observations"][-WINDOW_BARS:]
    if len(observations) < WINDOW_BARS:
        raise ForecastError("INSUFFICIENT_BARS")
    stamps = [live_ai._timestamp(o["event_at"]) for o in observations]
    if any((b - a).total_seconds() != BAR_SECONDS for a, b in zip(stamps, stamps[1:])):
        raise ForecastError("GAPPED_OR_REPEATED_BARS")
    age = (live_ai._timestamp(as_of) - stamps[-1]).total_seconds()
    if not 0 <= age <= max_bar_age_s:
        raise ForecastError("STALE_BARS")
    try:
        return [Decimal(o["price"]) for o in observations], [Decimal(o["volume"]) for o in observations]
    except InvalidOperation:
        raise ForecastError("WINDOW_VALUES_INVALID") from None


def validate_forecast_config(fc, enabled_universe: dict, lookback_bars: int) -> dict:
    """Config block `proposer.forecast`: a pinned artifact (absolute path + file SHA-256) per enabled market/symbol."""
    if not isinstance(fc, dict) or set(fc) != {"artifacts", "max_artifact_age_days"}:
        raise ValidationError("proposer.forecast 필드는 정확히 artifacts, max_artifact_age_days 입니다.")
    age = fc["max_artifact_age_days"]
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 90:
        raise ValidationError("proposer.forecast.max_artifact_age_days: 1~90 사이 정수여야 합니다 (기본값 없음).")
    if lookback_bars < WINDOW_BARS:
        raise ValidationError(f"시계열 예측에는 cycle.lookback_bars가 {WINDOW_BARS} 이상이어야 합니다.")
    artifacts = fc["artifacts"]
    if not isinstance(artifacts, dict) or not set(artifacts) <= {"KR", "US"}:
        raise ValidationError("proposer.forecast.artifacts는 KR/US 키만 허용합니다.")
    for market, symbols in enabled_universe.items():
        pins = artifacts.get(market)
        if not isinstance(pins, dict) or set(pins) != set(symbols):
            raise ValidationError(f"proposer.forecast.artifacts.{market}는 활성 종목 전체에 정확히 하나씩 있어야 합니다.")
    for market, pins in artifacts.items():
        if not isinstance(pins, dict):
            raise ValidationError(f"proposer.forecast.artifacts.{market} 형식이 올바르지 않습니다.")
        for symbol, pin in pins.items():
            if not isinstance(pin, dict) or set(pin) != {"path", "sha256"} or not isinstance(pin["path"], str) \
                    or not 1 <= len(pin["path"]) <= 400 or not Path(pin["path"]).is_absolute() \
                    or not isinstance(pin["sha256"], str) or not _SHA256.fullmatch(pin["sha256"]):
                raise ValidationError(f"proposer.forecast.artifacts.{market}.{symbol}: 절대 경로 path와 "
                                      "소문자 64자리 sha256이 필요합니다.")
    return fc


def live_forecast(snapshot: dict, forecast_cfg, *, quotes: dict, costs: dict, max_bar_age_s: int,
                  session_date: str) -> tuple[dict, dict]:
    """Forecast object for every candidate of a validated LIVE snapshot, plus an audit record.

    Raises ForecastError when the block is absent, an artifact is missing/mismatched/stale or a window is
    insufficient: the caller then HOLDs without calling the model. `quotes`: symbol -> {"bid", "ask"}.
    """
    if forecast_cfg is None:
        raise ForecastError("FORECAST_NOT_CONFIGURED")
    safe = live_ai.validate_snapshot(snapshot)
    market = safe["market"]
    pins = forecast_cfg["artifacts"].get(market) or {}
    try:
        today = date.fromisoformat(session_date)
    except (TypeError, ValueError):
        raise ForecastError("SESSION_DATE_INVALID") from None
    entries, audit = [], {}
    for candidate in safe["candidates"]:
        symbol = candidate["symbol"]
        try:
            pin = pins.get(symbol)
            if pin is None:
                raise ForecastError("NO_PINNED_ARTIFACT")
            artifact = load_artifact(pin["path"], expected_sha256=pin["sha256"], market=market, symbol=symbol)
            trained_through = date.fromisoformat(artifact["train"]["last_session"])
            if trained_through >= today:
                raise ForecastError("ARTIFACT_NOT_STRICTLY_BEFORE_SESSION")
            if (today - trained_through).days > forecast_cfg["max_artifact_age_days"]:
                raise ForecastError("ARTIFACT_STALE")
            closes, volumes = snapshot_window(candidate, safe["as_of"], max_bar_age_s)
            gross = predict(artifact, features(closes, volumes))
            quote = quotes.get(symbol)
            if quote is None:
                raise ForecastError("NO_QUOTE")
            cost = live_research.hurdle_bps(market, quote["bid"], quote["ask"], costs)["hurdle_bps"]
        except ForecastError as exc:
            raise ForecastError(f"{symbol}:{exc}") from None
        except ValidationError as exc:
            raise ForecastError(f"{symbol}:{str(exc)[:80]}") from None
        entries.append(forecast_entry(artifact, symbol, gross, cost))
        audit[symbol] = {"artifact_sha256": pin["sha256"], "content_sha256": artifact["content_sha256"],
                         "train_last_session": artifact["train"]["last_session"]}
    forecast = forecast_object(entries)
    live_ai.validate_forecast(forecast, safe)
    return forecast, audit


# ---------------------------------------------------------------- CLI

def split_sessions(bars, train_end_session: str) -> tuple[list[str], list[str]]:
    if not isinstance(train_end_session, str) or not _SESSION.fullmatch(train_end_session):
        raise TrainingError("--train-end-session must be YYYY-MM-DD")
    sessions = _session_list(bars)
    return [s for s in sessions if s <= train_end_session], [s for s in sessions if s > train_end_session]


def holdout_diagnostic(artifact: dict, bars, holdout_sessions) -> dict:
    rows = samples(bars, set(holdout_sessions))
    predicted = [_linear(artifact["intercept"], artifact["coefficients"], artifact["scaler"]["means"],
                         artifact["scaler"]["scales"], r["x"]) for r in rows]
    return {"sessions": len(set(holdout_sessions)), **diagnostics(predicted, [r["y"] for r in rows]),
            "used_for_fitting_or_tuning": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stocklab.ts_forecast",
                                     description="Train the ARX/ridge forecaster offline from local minute bars.")
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("train", help="fit on sessions <= --train-end-session and write an immutable artifact")
    tr.add_argument("--input", required=True, help="local CSV: " + ",".join(he.COLUMNS))
    tr.add_argument("--output", required=True, help="new artifact path (never overwritten); keep it git-ignored")
    tr.add_argument("--train-end-session", required=True,
                    help="last training session (local date YYYY-MM-DD); later sessions are holdout only")
    tr.add_argument("--expected-source", default=he.DEFAULT_SOURCE)
    tr.add_argument("--drop-outside-session", action="store_true")
    args = parser.parse_args(argv)
    try:
        check_private_output(args.output)
        if Path(args.output).resolve() == Path(args.input).resolve():
            raise TrainingError("output must differ from input")
        data = he.load_bars(args.input, expected_source=args.expected_source,
                            drop_outside_session=args.drop_outside_session)
        train_sessions, holdout_sessions = split_sessions(data["bars"], args.train_end_session)
        artifact = train(data, train_sessions)
        file_sha = save_artifact(artifact, args.output)
    except FileExistsError:
        print("ts_forecast: output exists; artifacts are immutable (choose a new path)", file=sys.stderr)
        return 2
    except (ValidationError, OSError, UnicodeDecodeError) as exc:
        print(f"ts_forecast: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    summary = {"model_version": MODEL_VERSION, "market": artifact["market"], "symbol": artifact["symbol"],
               "train": artifact["train"], "residual": artifact["residual"], "artifact_sha256": file_sha,
               "holdout_diagnostic": holdout_diagnostic(artifact, data["bars"], holdout_sessions)
               if holdout_sessions else None,
               "notice": NOTICE}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
