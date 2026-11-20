"""Hover readouts on the server-side SVG charts (CSS only, no JavaScript)."""

import re

from app.charts import line_chart

POINTS = [("2026-11-17", 40.0), ("2026-11-18", 55.5), ("2026-11-19", 70.0)]


def test_one_hover_group_per_point():
    svg = str(line_chart(POINTS, suffix="%"))
    assert svg.count('<g class="pt">') == 3
    assert svg.count('class="hit"') == 3
    assert svg.count('class="guide"') == 3


def test_readout_shows_date_and_value():
    svg = str(line_chart(POINTS, suffix="%"))
    tips = re.findall(r'<text class="tip"[^>]*>([^<]*)</text>', svg)
    assert tips == ["2026-11-17 · 40%", "2026-11-18 · 55.5%", "2026-11-19 · 70%"]


def test_readout_stays_inside_the_chart():
    svg = str(line_chart(POINTS, width=640))
    for x in re.findall(r'<text class="tip" x="([\d.]+)"', svg):
        assert 0 < float(x) < 640


def test_hit_areas_cover_the_plot_without_overlap():
    svg = str(line_chart(POINTS, width=640))
    rects = [
        (float(x), float(w))
        for x, w in re.findall(r'<rect class="hit" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
    ]
    for (x1, w1), (x2, _) in zip(rects, rects[1:]):
        assert abs((x1 + w1) - x2) < 0.2


def test_values_are_escaped():
    svg = str(line_chart([("<b>", 1.0)], suffix="<%"))
    assert "<b>" not in svg
    assert "&lt;b&gt;" in svg
    assert "&lt;%" in svg


def test_single_point_and_empty():
    assert str(line_chart([("2026-11-19", 50.0)])).count('<g class="pt">') == 1
    assert "No history yet" in str(line_chart([]))
