"""Report filenames are stamped in US Eastern (market) time, whatever clock runs the script.

The dashboard groups history by the date in the filename, so an evening run must
not land on the next day just because it ran in a UTC container.
"""

from datetime import datetime, timezone

from screen_vcp import report_stamp


def test_evening_run_keeps_its_trading_day():
    # 04:03 UTC on Nov 19 is 11:03 PM EST on Nov 18.
    assert report_stamp(datetime(2026, 11, 19, 4, 3, tzinfo=timezone.utc)) == "2026-11-18_230300"


def test_daylight_saving_time():
    # 20:20 UTC on Jul 1 is 4:20 PM EDT.
    assert report_stamp(datetime(2026, 7, 1, 20, 20, tzinfo=timezone.utc)) == "2026-07-01_162000"


def test_defaults_to_now():
    assert len(report_stamp()) == len("2026-11-18_230300")
