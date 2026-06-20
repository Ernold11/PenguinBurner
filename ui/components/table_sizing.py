from __future__ import annotations


def set_header_fit_column_widths(
    table,
    widths: dict[int, int],
    *,
    QtCore,
    padding: int = 32,
    samples: dict[int, str] | None = None,
) -> None:
    """Size each column to fit its contents.

    A column is made just wide enough for the wider of its header label and its
    widest expected cell value (``samples``), plus ``padding``. ``widths`` acts
    only as a lower bound, so columns adapt to content instead of reserving a
    fixed, oversized amount of space.
    """

    header = table.horizontalHeader()
    model = table.model()
    metrics = header.fontMetrics()
    samples = samples or {}
    for column, requested_width in widths.items():
        label = str(model.headerData(column, QtCore.Qt.Horizontal) or "")
        text_width = metrics.horizontalAdvance(label) + int(padding)
        sample = samples.get(int(column))
        sample_width = (
            metrics.horizontalAdvance(str(sample)) + int(padding) if sample else 0
        )
        table.setColumnWidth(
            int(column),
            max(int(requested_width), int(text_width), int(sample_width)),
        )
