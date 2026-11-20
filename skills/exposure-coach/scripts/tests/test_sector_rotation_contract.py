"""sector-analyst's analyze_sector_rotation.py JSON feeds the sector input."""

from calculate_exposure import extract_sector_score


def _report(score=55, is_fresh=True):
    return {
        "meta": {"freshness": {"date": "2026-11-18", "is_fresh": is_fresh, "warning": None}},
        "groups": {"score": score, "regime": "balanced"},
        "ranking": [{"rank": 1, "sector": "Healthcare"}],
    }


def test_reads_groups_score():
    assert extract_sector_score(_report(55)) == 55


def test_rounds_and_clamps():
    assert extract_sector_score(_report(72.6)) == 73
    assert extract_sector_score(_report(130)) == 100


def test_stale_csv_counts_as_missing():
    assert extract_sector_score(_report(55, is_fresh=False)) is None


def test_non_numeric_score_counts_as_missing():
    assert extract_sector_score(_report("N/A")) is None
    assert extract_sector_score(_report(True)) is None


def test_legacy_sector_score_still_wins():
    data = _report(55)
    data["sector_score"] = 80
    assert extract_sector_score(data) == 80
