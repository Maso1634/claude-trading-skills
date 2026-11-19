"""notify_refresh.py: one ntfy push when the refresh job needs attention."""

from __future__ import annotations

import json

import notify_refresh as nr


def report(**over):
    base = {
        "metadata": {
            "data_source": "auto",
            "api_stats": {"data_sources": {"alpaca": 503}, "source_errors": {}},
        },
        "results": [{"symbol": "AAA", "cross_check": {"status": "ok"}}],
        "evaluation_coverage": {"universe_size": 503, "not_evaluated": 0},
    }
    base.update(over)
    return base


def test_clean_run_sends_nothing():
    assert nr.build_message([], report()) == ("", "", "")


def test_failed_step_is_high_priority():
    title, body, prio = nr.build_message(["breadth"], None)
    assert "problem" in title and prio == "high"
    assert "breadth" in body


def test_backup_source_and_mismatch_are_reported():
    r = report(
        metadata={
            "data_source": "auto",
            "api_stats": {"data_sources": {"alpaca": 500, "yfinance": 3}, "source_errors": {}},
        },
        results=[
            {"symbol": "ODDD", "cross_check": {"status": "mismatch", "price_diff_pct": 3.2}},
        ],
    )
    title, body, prio = nr.build_message([], r)
    assert "check data" in title and prio == "default"
    assert "yfinance for 3" in body
    assert "ODDD (mismatch, price off 3.2%)" in body


def test_source_failure_and_unevaluated_are_serious():
    r = report(
        metadata={
            "data_source": "auto",
            "api_stats": {
                "data_sources": {"yfinance": 503},
                "source_errors": {"alpaca": "HTTP 403"},
            },
        },
        evaluation_coverage={
            "universe_size": 503,
            "not_evaluated": 12,
            "not_evaluated_reasons": {"missing_data": 12},
        },
    )
    title, body, prio = nr.build_message([], r)
    assert prio == "high"
    assert "alpaca failed: HTTP 403" in body
    assert "12 of 503 stocks not evaluated" in body


def test_main_posts_to_ntfy_with_token(monkeypatch, tmp_path):
    path = tmp_path / "vcp.json"
    path.write_text(json.dumps(report()), encoding="utf-8")
    sent = {}

    def fake_send(url, title, body, priority, token=""):
        sent.update(url=url, title=title, token=token)

    monkeypatch.setattr(nr, "send", fake_send)
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setenv("NTFY_TOKEN", "tk_fixture")  # pragma: allowlist secret
    assert nr.main(["--failed", "uptrend", "--vcp-report", str(path)]) == 0
    assert sent["url"] == "https://ntfy.example/topic"
    assert sent["token"] == "tk_fixture"  # pragma: allowlist secret


def test_main_without_ntfy_url_is_quiet_success(monkeypatch, capsys):
    monkeypatch.delenv("NTFY_URL", raising=False)
    assert nr.main(["--failed", "vcp"]) == 0
    assert "NTFY_URL not set" in capsys.readouterr().out
