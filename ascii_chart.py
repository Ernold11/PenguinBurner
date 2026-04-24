#!/usr/bin/env python3


def _chart_axis_bounds(values, *, rounding, include_zero=False):
    filtered = [float(value) for value in values if value is not None]
    if not filtered:
        lower = 0.0
        upper = float(rounding)
    else:
        lower = min(filtered)
        upper = max(filtered)
        if include_zero:
            lower = min(lower, 0.0)
            upper = max(upper, 0.0)
        lower = float(int(lower // rounding) * rounding)
        upper = float(int((upper + rounding - 1) // rounding) * rounding)
        if upper <= lower:
            upper = lower + float(rounding)
    return lower, upper


def _chart_merge_char(existing, new):
    if existing == " ":
        return new
    if existing == new:
        return existing
    if new == "@":
        return "@"
    if existing == "@":
        return "@"
    if existing == "#" or new == "#":
        return "#"
    return new


def _plot_chart_point(grid, row, col, char):
    if 0 <= row < len(grid) and 0 <= col < len(grid[row]):
        grid[row][col] = _chart_merge_char(grid[row][col], char)


def _map_chart_x(value, lower, upper, width):
    if width <= 1 or upper <= lower:
        return 0
    return int(round((float(value) - lower) * float(width - 1) / float(upper - lower)))


def _map_chart_y(value, lower, upper, height):
    if height <= 1 or upper <= lower:
        return 0
    scaled = (float(value) - lower) * float(height - 1) / float(upper - lower)
    return int(round(float(height - 1) - scaled))


def render_line_chart(
    title,
    *,
    series,
    x_label,
    y_label,
    x_rounding,
    y_rounding,
    width=56,
    height=12,
    include_zero_y=False,
    highlights=None,
):
    all_points = [
        (float(point[0]), float(point[1]))
        for item in series
        for point in item.get("points", [])
    ]
    if not all_points:
        return [f"{title}: unavailable"]

    x_values = [point[0] for point in all_points]
    y_values = [point[1] for point in all_points]
    x_lower, x_upper = _chart_axis_bounds(x_values, rounding=x_rounding)
    y_lower, y_upper = _chart_axis_bounds(
        y_values,
        rounding=y_rounding,
        include_zero=include_zero_y,
    )

    grid = [[" "] * int(width) for _ in range(int(height))]
    for item in series:
        points = [
            (float(point[0]), float(point[1])) for point in item.get("points", [])
        ]
        if not points:
            continue
        char = str(item.get("char", "#"))[:1] or "#"
        for left_point, right_point in zip(points, points[1:]):
            col0 = _map_chart_x(left_point[0], x_lower, x_upper, width)
            row0 = _map_chart_y(left_point[1], y_lower, y_upper, height)
            col1 = _map_chart_x(right_point[0], x_lower, x_upper, width)
            row1 = _map_chart_y(right_point[1], y_lower, y_upper, height)
            steps = max(abs(col1 - col0), abs(row1 - row0), 1)
            for step in range(steps + 1):
                col = int(round(col0 + (col1 - col0) * step / float(steps)))
                row = int(round(row0 + (row1 - row0) * step / float(steps)))
                _plot_chart_point(grid, row, col, char)
        for point in points:
            col = _map_chart_x(point[0], x_lower, x_upper, width)
            row = _map_chart_y(point[1], y_lower, y_upper, height)
            _plot_chart_point(grid, row, col, char)

    for highlight in highlights or []:
        x_value = highlight.get("x")
        y_value = highlight.get("y")
        if x_value is None or y_value is None:
            continue
        char = str(highlight.get("char", "@"))[:1] or "@"
        col = _map_chart_x(x_value, x_lower, x_upper, width)
        row = _map_chart_y(y_value, y_lower, y_upper, height)
        _plot_chart_point(grid, row, col, char)

    label_width = max(len(str(int(round(y_lower)))), len(str(int(round(y_upper)))), 3)
    tick_rows = {}
    tick_count = min(max(int(height // 2), 4), int(height))
    for index in range(tick_count):
        value = y_upper - (y_upper - y_lower) * float(index) / float(
            max(tick_count - 1, 1)
        )
        value = round(value / float(y_rounding)) * float(y_rounding)
        row = _map_chart_y(value, y_lower, y_upper, height)
        tick_rows[row] = int(round(value))

    chart_lines = [title]
    for row_index, row_cells in enumerate(grid):
        if row_index in tick_rows:
            label = f"{tick_rows[row_index]:>{label_width}d}"
        else:
            label = " " * label_width
        chart_lines.append(f"{label} |{''.join(row_cells)}")
    chart_lines.append(f"{' ' * label_width} +{'-' * int(width)}")

    x_tick_count = 5
    x_tick_line = [" "] * int(width)
    for index in range(x_tick_count):
        value = x_lower + (x_upper - x_lower) * float(index) / float(
            max(x_tick_count - 1, 1)
        )
        label = str(int(round(value)))
        col = int(round(index * float(width - 1) / float(max(x_tick_count - 1, 1))))
        start = max(0, min(int(width) - len(label), col - len(label) // 2))
        end = start + len(label)
        if any(x_tick_line[position] != " " for position in range(start, end)):
            continue
        for offset, char in enumerate(label):
            x_tick_line[start + offset] = char
    chart_lines.append(f"{' ' * label_width}  {''.join(x_tick_line)} {x_label}")
    return chart_lines
