"""Read-only access to the skills' report files and trader-memory-core state.

Every function here only reads files. Nothing writes to reports/ or state/.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

# key -> (label, filename prefix). Prefixes include the trailing underscore so
# "vcp_screener_" never matches "vcp_history_".
REPORT_TYPES: dict[str, tuple[str, str]] = {
    "exposure": ("Exposure posture", "exposure_posture_"),
    "breadth": ("Market breadth", "market_breadth_"),
    "uptrend": ("Uptrend ratio", "uptrend_analysis_"),
    "regime": ("Macro regime", "macro_regime_"),
    "top_risk": ("Market top risk", "market_top_"),
    "ftd": ("Follow-Through Day", "ftd_detector_"),
    "sector": ("Sector rotation", "sector_rotation_"),
    # "theme_detector_2" (the date) skips theme_detector_history.json.
    "theme": ("Themes", "theme_detector_2"),
    "vcp": ("VCP screener", "vcp_screener_"),
    "breakout": ("Breakout plan", "breakout_trade_plan_"),
    "circuit": ("Circuit breaker", "circuit_breaker_decision_"),
    "digest": ("Weekly digest", "weekly_digest_"),
}

TERMINAL_STATUSES = {"CLOSED", "INVALIDATED"}  # matches trader-memory-core
VIEWABLE_SUFFIXES = {".md", ".json"}
_STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:_(\d{6}))?")


def parse_stamp(name: str) -> datetime | None:
    """Pull the YYYY-MM-DD[_HHMMSS] stamp the skills put in their filenames."""
    match = _STAMP_RE.search(name)
    if not match:
        return None
    day, clock = match.groups()
    try:
        return datetime.strptime(day + (clock or "000000"), "%Y-%m-%d%H%M%S")
    except ValueError:
        return None


def file_time(path: Path) -> datetime | None:
    """When the file was written, as an aware UTC datetime.

    Shown on cards instead of the filename stamp: some skills stamp filenames with
    the local clock and others with UTC, so the filename can be hours off.
    """
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    except OSError:
        return None


def age_hours(stamp: datetime, now: datetime | None = None) -> float:
    """Hours since a stamp (aware, or naive meaning UTC). Used only for a 'stale' badge."""
    now = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds() / 3600


# The refresh cron runs at 4:20 PM ET on trading days. A report counts as stale
# only when a scheduled run that should have replaced it has come and gone, so
# Friday's report stays fresh over the weekend and on market holidays.
MARKET_TZ = ZoneInfo("America/New_York")
REFRESH_TIME = time(16, 20)
REFRESH_GRACE = timedelta(hours=2)  # time for the run to finish before we complain
EARLY_SLACK = timedelta(hours=1)  # a hand run shortly before 4:20 still counts


@lru_cache(maxsize=32)
def _market_holidays(start: date, end: date) -> frozenset[date]:
    """NYSE weekday closures between start and end. Empty if the calendar is unavailable."""
    try:
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        open_days = {d.date() for d in nyse.valid_days(start_date=start, end_date=end)}
    except Exception:  # missing package or calendar data: treat every weekday as a session
        return frozenset()
    days = (start + timedelta(n) for n in range((end - start).days + 1))
    return frozenset(d for d in days if d.weekday() < 5 and d not in open_days)


def last_due_refresh(now: datetime | None = None) -> datetime:
    """The most recent scheduled refresh (4:20 PM ET, trading days) whose grace period has passed."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_et = now.astimezone(MARKET_TZ)
    day = now_et.date()
    # Look up a month at a time so the cache is reused across requests.
    holidays = _market_holidays(
        day.replace(day=1) - timedelta(days=31), day.replace(day=1) + timedelta(days=31)
    )
    for back in range(15):
        d = day - timedelta(days=back)
        if d.weekday() >= 5 or d in holidays:
            continue
        run = datetime.combine(d, REFRESH_TIME, tzinfo=MARKET_TZ)
        if run + REFRESH_GRACE <= now_et:
            return run
    return datetime.combine(day - timedelta(days=15), REFRESH_TIME, tzinfo=MARKET_TZ)


def is_stale(stamp: datetime, now: datetime | None = None) -> bool:
    """True when a scheduled refresh should have replaced this report (stamp naive = UTC)."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < last_due_refresh(now) - EARLY_SLACK


def _sort_key(path: Path) -> tuple[datetime, str]:
    return (parse_stamp(path.name) or datetime.min, path.name)


def list_reports(reports_dir: Path, key: str, suffix: str = ".json") -> list[Path]:
    """All report files of one type, newest first. Looks in subfolders too."""
    prefix = REPORT_TYPES[key][1]
    if not Path(reports_dir).is_dir():
        return []
    files = [
        p
        for p in Path(reports_dir).rglob(f"{prefix}*{suffix}")
        if p.is_file() and p.name.startswith(prefix) and parse_stamp(p.name[len(prefix) :])
    ]
    return sorted(files, key=_sort_key, reverse=True)


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def latest(reports_dir: Path, key: str) -> dict[str, Any] | None:
    """Newest readable report of a type, with its stamp and markdown sibling."""
    for path in list_reports(reports_dir, key):
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        md = path.with_suffix(".md")
        return {
            "path": path,
            "name": path.relative_to(reports_dir).as_posix(),
            "md_name": md.relative_to(reports_dir).as_posix() if md.exists() else None,
            "stamp": file_time(path),
            "data": data,
        }
    return None


def dig(data: Any, dotted: str) -> Any:
    """dig({"a": {"b": 1}}, "a.b") -> 1, or None when any step is missing."""
    for part in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


HISTORY_FIELDS: dict[str, str] = {
    "exposure": "exposure_ceiling_pct",
    "breadth": "composite.composite_score",
    "uptrend": "composite.composite_score",
}


def history(
    reports_dir: Path,
    key: str,
    field: str | None = None,
    limit: int = 90,
    extractor: Callable[[dict], Any] | None = None,
) -> list[tuple[str, float]]:
    """One (YYYY-MM-DD, value) point per day, oldest first, latest run of each day wins."""
    field = field or HISTORY_FIELDS[key]
    by_day: dict[str, float] = {}
    for path in list_reports(reports_dir, key):  # newest first
        stamp = parse_stamp(path.name)
        if stamp is None:
            continue
        day = stamp.strftime("%Y-%m-%d")
        if day in by_day:
            continue
        data = load_json(path)
        value = extractor(data) if extractor else dig(data, field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            by_day[day] = value
        if len(by_day) >= limit:
            break
    return sorted(by_day.items())


def all_reports(reports_dir: Path, per_type: int = 30) -> dict[str, list[dict[str, Any]]]:
    """Recent report files grouped by type, for the Reports page."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for key in REPORT_TYPES:
        rows = []
        for path in list_reports(reports_dir, key)[:per_type]:
            md = path.with_suffix(".md")
            rows.append(
                {
                    "name": path.relative_to(reports_dir).as_posix(),
                    "md_name": md.relative_to(reports_dir).as_posix() if md.exists() else None,
                    "stamp": file_time(path),
                }
            )
        if rows:
            groups[key] = rows
    return groups


def safe_report_path(reports_dir: Path, relname: str) -> Path | None:
    """Resolve a user-supplied report name, refusing anything outside reports_dir."""
    root = Path(reports_dir).resolve()
    try:
        candidate = (root / relname).resolve()
    except (OSError, ValueError):
        return None
    if root not in candidate.parents:
        return None
    if candidate.suffix not in VIEWABLE_SUFFIXES or not candidate.is_file():
        return None
    return candidate


# ---------------------------------------------------------------- theses


def load_theses(state_dir: Path) -> tuple[list[dict], list[str]]:
    """All th_*.yaml theses under state_dir (any subfolder), newest update first."""
    theses: list[dict] = []
    errors: list[str] = []
    root = Path(state_dir)
    if not root.is_dir():
        return theses, errors
    for path in sorted(root.rglob("th_*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            errors.append(path.name)
            continue
        if not isinstance(data, dict) or "thesis_id" not in data:
            errors.append(path.name)
            continue
        theses.append(data)
    theses.sort(key=lambda t: str(t.get("updated_at") or ""), reverse=True)
    return theses, errors


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def thesis_summary(theses: list[dict], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    by_status: dict[str, int] = {}
    review_due = []
    open_risk = 0.0
    closed_pnl = 0.0
    for t in theses:
        status = str(t.get("status") or "UNKNOWN")
        by_status[status] = by_status.get(status, 0) + 1
        terminal = status in TERMINAL_STATUSES
        next_review = dig(t, "monitoring.next_review_date")
        if not terminal and next_review:
            try:
                if date.fromisoformat(str(next_review)[:10]) <= today:
                    review_due.append(t)
            except ValueError:
                pass
        if status in {"ACTIVE", "PARTIALLY_CLOSED"}:
            open_risk += _num(dig(t, "position.risk_dollars")) or 0.0
        pnl = _num(dig(t, "outcome.pnl_dollars"))
        if pnl is not None:
            closed_pnl += pnl
    return {
        "by_status": by_status,
        "review_due": review_due,
        "open_risk_dollars": round(open_risk, 2),
        "closed_pnl_dollars": round(closed_pnl, 2),
    }


def find_thesis(theses: list[dict], thesis_id: str) -> dict | None:
    return next((t for t in theses if t.get("thesis_id") == thesis_id), None)


def journal_for(state_dir: Path, thesis_id: str) -> str | None:
    """The trader-memory-core postmortem (pm_<id>.md) for a thesis, if one exists."""
    root = Path(state_dir)
    if not root.is_dir() or not re.fullmatch(r"[A-Za-z0-9_.-]+", thesis_id):
        return None
    for path in root.rglob(f"pm_{thesis_id}.md"):
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return None
