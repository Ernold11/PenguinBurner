from __future__ import annotations


def set_header_fit_column_widths(
    table,
    widths: dict[int, int],
    *,
    QtCore,
    padding: int = 32,
) -> None:
    header = table.horizontalHeader()
    model = table.model()
    for column, requested_width in widths.items():
        label = str(model.headerData(column, QtCore.Qt.Horizontal) or "")
        text_width = header.fontMetrics().horizontalAdvance(label) + int(padding)
        table.setColumnWidth(int(column), max(int(requested_width), int(text_width)))
