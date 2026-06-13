"""Unit tests for the pure ascii line-chart renderer."""
from __future__ import annotations

from common.ascii_chart import (
    _chart_axis_bounds,
    _chart_merge_char,
    _map_chart_x,
    _map_chart_y,
    render_line_chart,
)


def test_axis_bounds_empty_uses_zero_to_rounding():
    assert _chart_axis_bounds([], rounding=10) == (0.0, 10.0)


def test_axis_bounds_rounds_outward_to_rounding_multiples():
    assert _chart_axis_bounds([12, 47], rounding=10) == (10.0, 50.0)


def test_axis_bounds_include_zero_extends_lower_bound():
    assert _chart_axis_bounds([5, 20], rounding=10, include_zero=True) == (0.0, 20.0)


def test_axis_bounds_bumps_upper_when_not_above_lower():
    lower, upper = _chart_axis_bounds([10, 10], rounding=10)
    assert lower == 10.0
    assert upper == 20.0
    assert upper > lower


def test_merge_char_precedence():
    assert _chart_merge_char(" ", "#") == "#"  # blank takes the new char
    assert _chart_merge_char("#", "#") == "#"  # identical stays
    assert _chart_merge_char("#", "@") == "@"  # highlight wins over line
    assert _chart_merge_char("@", "#") == "@"  # existing highlight is kept
    assert _chart_merge_char("#", ".") == "#"  # line wins over a lighter mark
    assert _chart_merge_char(".", "#") == "#"  # line wins regardless of order
    assert _chart_merge_char(".", "x") == "x"  # otherwise the new char wins


def test_map_chart_x_degenerate_inputs_return_zero():
    assert _map_chart_x(5, 0, 10, 1) == 0  # width too small
    assert _map_chart_x(5, 10, 10, 20) == 0  # zero-width range


def test_map_chart_x_maps_endpoints_to_grid_columns():
    assert _map_chart_x(0, 0, 10, 11) == 0
    assert _map_chart_x(5, 0, 10, 11) == 5
    assert _map_chart_x(10, 0, 10, 11) == 10


def test_map_chart_y_is_inverted_and_handles_degenerate_inputs():
    assert _map_chart_y(5, 0, 10, 1) == 0  # height too small
    assert _map_chart_y(5, 10, 10, 20) == 0  # zero-height range
    assert _map_chart_y(10, 0, 10, 11) == 0  # max value -> top row
    assert _map_chart_y(0, 0, 10, 11) == 10  # min value -> bottom row
    assert _map_chart_y(5, 0, 10, 11) == 5  # mid value -> middle row


def test_render_line_chart_unavailable_without_points():
    assert render_line_chart(
        "CHART",
        series=[],
        x_label="ms",
        y_label="C",
        x_rounding=10,
        y_rounding=10,
    ) == ["CHART: unavailable"]


def test_render_line_chart_renders_grid_axes_and_series():
    lines = render_line_chart(
        "CHART",
        series=[
            {"points": [(0, 0), (10, 10)], "char": "#"},
            {"points": [], "char": "x"},  # empty series is skipped
        ],
        x_label="ms",
        y_label="C",
        x_rounding=10,
        y_rounding=10,
        width=20,
        height=6,
    )
    # title + height rows + axis rule + x-tick line
    assert len(lines) == 1 + 6 + 1 + 1
    assert lines[0] == "CHART"
    assert any("#" in line for line in lines)  # series plotted
    assert any("+" in line and "-" in line for line in lines)  # axis rule
    assert lines[-1].endswith("ms")  # x label


def test_render_line_chart_plots_highlights_and_skips_incomplete_ones():
    lines = render_line_chart(
        "CHART",
        series=[{"points": [(0, 0), (10, 10)]}],  # default char "#"
        x_label="ms",
        y_label="C",
        x_rounding=10,
        y_rounding=10,
        width=20,
        height=6,
        include_zero_y=True,
        highlights=[
            {"x": 5, "y": 5},  # default highlight char "@"
            {"x": None, "y": 3},  # missing coordinate -> skipped
        ],
    )
    assert any("@" in line for line in lines)
