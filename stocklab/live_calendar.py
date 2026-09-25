"""Regular-session calendars for autonomous orders. Pure functions; no network, no guessing.

Trading days are NOT derived from weekday rules. The user supplies, inside the autonomous config, an
explicit calendar per market transcribed from the exchange's official publication (KRX 휴장일/매매시간
공지, NYSE/Nasdaq holidays and early closes) with a validity range. Inside that range a date without a
session entry is closed; outside it the state is unknown and orders are disabled.

Local session times are converted to UTC with:
  KR  fixed UTC+09:00 (Korea observes no DST).
  US  America/New_York by the statutory rule in force since 2007 (15 U.S.C. 260a: DST from the second
      Sunday of March to the first Sunday of November, changes at 02:00 local). The rule is only accepted
      for US_RULE_YEARS; if the standard-library zoneinfo database is available it must agree, otherwise
      the calendar is refused. Session times are never inside the 01:00-03:00 transition hours.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
import re

from .domain import ValidationError

KST = timezone(timedelta(hours=9))
CALENDAR_SCHEMA = "stocklab-session-calendar-v1"
US_RULE_YEARS = range(2007, 2031)
# Accepted local regular-session bounds. KRX: normal 09:00-15:30, first trading day of the year and the
# CSAT day open later and may close later; NYSE/Nasdaq: 09:30-16:00, early closes at 13:00.
SESSION_BOUNDS = {"KR": {"open": (dtime(9, 0), dtime(10, 0)), "close": (dtime(12, 0), dtime(16, 30))},
                  "US": {"open": (dtime(9, 30), dtime(9, 30)), "close": (dtime(13, 0), dtime(16, 0))}}
MAX_CALENDAR_DAYS = 200


class CalendarError(ValidationError):
    pass


def _nth_sunday(year, month, n):
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def us_is_dst(local_day: date) -> bool:
    """DST for a US-Eastern calendar day, valid for times after 03:00 local on transition days."""
    if local_day.year not in US_RULE_YEARS:
        raise CalendarError("미국 서머타임 규칙의 검증 범위를 벗어난 연도입니다. 주문하지 않습니다.")
    return _nth_sunday(local_day.year, 3, 2) <= local_day < _nth_sunday(local_day.year, 11, 1)


def _zoneinfo_check(local_dt: datetime, offset: timedelta):
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/New_York")
    except Exception:
        return  # no tz database on this machine (common on Windows without tzdata): statutory rule only
    if local_dt.replace(tzinfo=zone).utcoffset() != offset:
        raise CalendarError("서머타임 계산이 시스템 시간대 DB와 다릅니다. 주문하지 않습니다.")


def eastern_offset(local_day: date) -> timedelta:
    return timedelta(hours=-4) if us_is_dst(local_day) else timedelta(hours=-5)


def local_to_utc(market: str, day: date, at: dtime) -> datetime:
    if market == "KR":
        return datetime.combine(day, at, KST).astimezone(timezone.utc)
    if not dtime(3, 0) <= at <= dtime(23, 59, 59):
        raise CalendarError("미국 시각이 서머타임 전환 시간대(01:00~03:00)에 있습니다.")
    offset = eastern_offset(day)
    local = datetime.combine(day, at)
    _zoneinfo_check(local, offset)
    return (local - offset).replace(tzinfo=timezone.utc)


def utc_to_local(market: str, at_utc: datetime) -> datetime:
    """Aware local exchange time for an aware instant."""
    if market == "KR":
        return at_utc.astimezone(KST)
    utc = at_utc.astimezone(timezone.utc)
    # The local day decides the offset; try both and keep the self-consistent one.
    for hours in (-4, -5):
        local = (utc + timedelta(hours=hours)).replace(tzinfo=None)
        if local.time() < dtime(3, 0) and local.date().month in (3, 11):
            continue  # transition night: refuse rather than decide
        if eastern_offset(local.date()) == timedelta(hours=hours):
            _zoneinfo_check(local, timedelta(hours=hours))
            return local.replace(tzinfo=timezone(timedelta(hours=hours)))
    raise CalendarError("미국 동부 시각을 확정하지 못했습니다(서머타임 전환 구간).")


def _day(value, name) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise CalendarError(f"{name}는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CalendarError(f"{name} 날짜가 올바르지 않습니다.") from None


def _hhmm(value, name) -> dtime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value) or int(value[:2]) > 23:
        raise CalendarError(f"{name}는 HH:MM 형식이어야 합니다.")
    return dtime(int(value[:2]), int(value[3:]))


def validate_calendar(market: str, value) -> dict:
    """Validate one market calendar (the JSON stored in the autonomous config). Returns it unchanged."""
    keys = {"schema", "market", "source", "valid_from", "valid_to", "sessions"}
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != CALENDAR_SCHEMA \
            or value["market"] != market:
        raise CalendarError(f"{market} 캘린더 형식이 올바르지 않습니다 (schema {CALENDAR_SCHEMA}).")
    source = value["source"]
    if not isinstance(source, str) or not 5 <= len(source) <= 300 or any(ord(c) < 32 for c in source):
        raise CalendarError("캘린더 출처(공식 공지 문서명·URL)를 5~300자로 적으세요.")
    start, end = _day(value["valid_from"], "valid_from"), _day(value["valid_to"], "valid_to")
    if end < start or (end - start).days > MAX_CALENDAR_DAYS:
        raise CalendarError(f"캘린더 유효 기간은 {MAX_CALENDAR_DAYS}일 이하여야 합니다.")
    sessions = value["sessions"]
    if not isinstance(sessions, list) or not sessions:
        raise CalendarError("캘린더에 거래일이 없습니다.")
    bounds, previous = SESSION_BOUNDS[market], None
    for entry in sessions:
        if not isinstance(entry, dict) or set(entry) != {"date", "open", "close"}:
            raise CalendarError("거래일 항목은 date/open/close만 가집니다.")
        day = _day(entry["date"], "date")
        opened, closed = _hhmm(entry["open"], "open"), _hhmm(entry["close"], "close")
        if not start <= day <= end or day.weekday() >= 5 or (previous is not None and day <= previous):
            raise CalendarError("거래일은 유효 기간 안의 평일이며 중복 없이 오름차순이어야 합니다.")
        if not bounds["open"][0] <= opened <= bounds["open"][1] or not bounds["close"][0] <= closed <= bounds["close"][1] \
                or closed <= opened:
            raise CalendarError(f"{entry['date']} 정규장 시각이 {market} 허용 범위를 벗어났습니다.")
        if market == "US":
            local_to_utc("US", day, opened)  # raises outside the verified DST-rule years
        previous = day
    return value


def session_state(market: str, calendar: dict, at_utc: datetime, *, open_buffer_min: int,
                  close_buffer_min: int) -> dict:
    """Where `at_utc` falls in the calendar. Only state TRADING_WINDOW permits a new order."""
    local = utc_to_local(market, at_utc)
    day = local.date()
    result = {"market": market, "session_date": day.isoformat(), "local_time": local.isoformat(timespec="seconds")}
    if not _day(calendar["valid_from"], "valid_from") <= day <= _day(calendar["valid_to"], "valid_to"):
        return {**result, "state": "CALENDAR_NOT_COVERING"}
    entry = next((s for s in calendar["sessions"] if s["date"] == day.isoformat()), None)
    if entry is None:
        return {**result, "state": "CLOSED_DAY"}
    open_utc = local_to_utc(market, day, _hhmm(entry["open"], "open"))
    close_utc = local_to_utc(market, day, _hhmm(entry["close"], "close"))
    start = open_utc + timedelta(minutes=open_buffer_min)
    end = close_utc - timedelta(minutes=close_buffer_min)
    result.update({"open_utc": open_utc.isoformat(), "close_utc": close_utc.isoformat(),
                   "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat()})
    at = at_utc.astimezone(timezone.utc)
    if at < start:
        state = "BEFORE_WINDOW"
    elif at >= end:
        state = "AFTER_WINDOW"
    else:
        state = "TRADING_WINDOW"
    return {**result, "state": state}


def resolve_stamp(date8: str, time6: str, now_utc: datetime, *, max_age_s: int, skew_s: int = 60):
    """Broker timestamp without a documented time zone -> (utc, basis) by freshness alone.

    Both readings (KST and US-Eastern) are tried; KST and US-Eastern differ by 13-14 hours, so at most one
    reading can fall within [now - max_age, now + skew] when max_age is minutes. Zero fresh readings means
    stale or unknown: the caller abstains. Nothing is chosen by preference.
    """
    if not re.fullmatch(r"[0-9]{8}", date8 or "") or not re.fullmatch(r"[0-2][0-9][0-5][0-9][0-5][0-9]", time6 or ""):
        raise CalendarError("MALFORMED_TIMESTAMP")
    if max_age_s > 3 * 3600:
        raise CalendarError("freshness window too wide to separate time zones")
    try:
        day = date(int(date8[:4]), int(date8[4:6]), int(date8[6:]))
        at = dtime(int(time6[:2]), int(time6[2:4]), int(time6[4:]))
    except ValueError:
        raise CalendarError("MALFORMED_TIMESTAMP") from None
    readings = [(datetime.combine(day, at, KST).astimezone(timezone.utc), "KST")]
    try:
        readings.append((local_to_utc("US", day, at), "US_EASTERN"))
    except CalendarError:
        pass  # transition hours or unverified year: that reading is not available
    now = now_utc.astimezone(timezone.utc)
    fresh = [(t, b) for t, b in readings
             if now - timedelta(seconds=max_age_s) <= t <= now + timedelta(seconds=skew_s)]
    if len(fresh) != 1:
        raise CalendarError("STALE_OR_UNKNOWN_TIME_BASIS")
    return fresh[0]
