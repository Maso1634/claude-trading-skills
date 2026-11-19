"""Send an ntfy push about problems in the market refresh job.

Called by refresh_market.sh at the end of a run. It reads the failed steps the
script collected, plus the newest VCP report written during this run, and
sends one push if anything needs attention. Quiet when everything worked.

Settings (.env):
  NTFY_URL    full topic URL, e.g. https://ntfy.sh/your-topic or
              https://ntfy.lab.example/trading (empty = no pushes)
  NTFY_TOKEN  access token if the topic needs one (optional)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

FLAG_STATUSES = ("mismatch", "volume_mismatch", "stale")


def vcp_problems(report: dict, primary: str = "alpaca") -> list[str]:
    """Human-readable problems in one VCP report."""
    problems = []
    meta = report.get("metadata") or {}
    stats = meta.get("api_stats") or {}
    for name, err in (stats.get("source_errors") or {}).items():
        problems.append(f"{name} failed: {err}")
    sources = stats.get("data_sources") or {}
    if meta.get("data_source") == "auto":
        backup = {k: v for k, v in sources.items() if k != primary}
        if backup:
            used = ", ".join(f"{k} for {v}" for k, v in sorted(backup.items()))
            problems.append(f"backup data used: {used} stocks")
    cov = report.get("evaluation_coverage") or {}
    if cov.get("not_evaluated"):
        reasons = ", ".join(f"{k} {v}" for k, v in (cov.get("not_evaluated_reasons") or {}).items())
        problems.append(
            f"{cov['not_evaluated']} of {cov.get('universe_size', '?')} stocks not evaluated ({reasons})"
        )
    flagged = [
        f"{r.get('symbol')} ({(r.get('cross_check') or {}).get('status', '').replace('_', ' ')}"
        f"{', price off ' + str(r['cross_check']['price_diff_pct']) + '%' if (r.get('cross_check') or {}).get('price_diff_pct') else ''})"
        for r in report.get("results") or []
        if (r.get("cross_check") or {}).get("status") in FLAG_STATUSES
    ]
    if flagged:
        problems.append("sources disagree, check the chart: " + "; ".join(flagged))
    return problems


def build_message(failed_steps: list[str], vcp_report: dict | None) -> tuple[str, str, str]:
    """(title, body, priority) or ("", "", "") when there is nothing to say."""
    lines = []
    if failed_steps:
        lines.append("Failed steps: " + ", ".join(failed_steps))
    vcp = vcp_problems(vcp_report) if vcp_report else []
    lines += [f"VCP: {p}" for p in vcp]
    if not lines:
        return "", "", ""
    serious = bool(failed_steps) or any("failed" in p or "not evaluated" in p for p in vcp)
    title = "Trading refresh: problem" if serious else "Trading refresh: check data"
    return title, "\n".join(lines), "high" if serious else "default"


def send(url: str, title: str, body: str, priority: str, token: str = "") -> None:
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    req.add_header("Tags", "chart_with_downwards_trend")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (URL from .env)
        resp.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--failed", default="", help="space-separated failed step names")
    parser.add_argument("--vcp-report", default="", help="VCP JSON written during this run")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args(argv)

    report = None
    if args.vcp_report and Path(args.vcp_report).is_file():
        try:
            report = json.loads(Path(args.vcp_report).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            args.failed += " vcp-report-unreadable"
    title, body, priority = build_message(args.failed.split(), report)
    if not title:
        print("notify: nothing to report")
        return 0
    print(f"notify: {title}\n{body}")
    url = os.environ.get("NTFY_URL", "").strip()
    if args.dry_run:
        return 0
    if not url:
        print("notify: NTFY_URL not set, no push sent")
        return 0
    try:
        send(url, title, body, priority, os.environ.get("NTFY_TOKEN", "").strip())
    except OSError as e:
        print(f"notify: push failed: {type(e).__name__}", file=sys.stderr)
        return 1
    print("notify: push sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
