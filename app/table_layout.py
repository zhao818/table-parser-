"""
Table layout normalizer — aligns multi-section grids for Excel rendering.

Problem: Qwen outputs sections with different column counts (e.g., header has
4 cells, body has 6 cells, footer has 6 cells). When parsed into a single grid,
the shorter sections get empty placeholder columns.

Solution:
1. Group rows into sections by their total colspan sum
2. Find the reference total (max across all sections)
3. Normalize all sections to share the same total colspan by scaling
   colspan values proportionally (preserving relative widths within sections)
4. Compute column widths from OCR pixel positions
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def normalize_layout(
    table: dict[str, Any],
    ocr_items: list[dict] | None = None,
) -> dict[str, Any]:
    """Normalize table grid so all rows share the same total colspan.

    Returns normalized table dict with added 'column_widths' key (list of floats).
    """
    rows = table.get("rows", [])
    if not rows:
        return table

    # ---- Step 1: Group rows into sections by colspan total ----
    sections: list[list[int]] = []  # each section = list of row indices
    current_section: list[int] = []
    prev_total = -1

    for i, row in enumerate(rows):
        row_total = sum(c.get("colspan", 1) for c in row)
        if row_total != prev_total:
            if current_section:
                sections.append(current_section)
            current_section = [i]
            prev_total = row_total
        else:
            current_section.append(i)
    if current_section:
        sections.append(current_section)

    if len(sections) <= 1:
        # Only one section — no normalization needed, just add column widths
        result = dict(table)
        result["column_widths"] = _compute_widths(rows, ocr_items)
        return result

    # ---- Step 2: Find reference total (max colspan across sections) ----
    section_totals: dict[int, int] = {}  # section_index -> colspan total
    max_total = 0
    for sec_idx, row_indices in enumerate(sections):
        total = sum(c.get("colspan", 1) for c in rows[row_indices[0]])
        section_totals[sec_idx] = total
        max_total = max(max_total, total)

    logger.info(
        "Layout: %d sections, totals=%s, reference=%d",
        len(sections),
        [section_totals[i] for i in range(len(sections))],
        max_total,
    )

    # ---- Step 3: Normalize all sections to max_total ----
    normalized_rows: list[list[dict[str, Any]]] = []

    for sec_idx, row_indices in enumerate(sections):
        sec_total = section_totals[sec_idx]
        if sec_total == max_total:
            # Already at reference width — use as-is
            for ri in row_indices:
                normalized_rows.append([dict(c) for c in rows[ri]])
        else:
            scale = max_total / sec_total
            for ri in row_indices:
                new_row: list[dict[str, Any]] = []
                allocated = 0
                for j, cell in enumerate(rows[ri]):
                    # Scale colspan proportionally, distribute rounding error
                    raw = cell.get("colspan", 1) * scale
                    if j == len(rows[ri]) - 1:
                        cs = max_total - allocated  # last cell gets remainder
                    else:
                        cs = max(1, round(raw))
                        allocated += cs
                    new_row.append({
                        "text": cell.get("text", ""),
                        "rowspan": cell.get("rowspan", 1),
                        "colspan": cs,
                    })
                normalized_rows.append(new_row)

    # ---- Step 4: Compute column widths ----
    column_widths = _compute_widths(normalized_rows, ocr_items)

    return {
        "title": table.get("title", ""),
        "rows": normalized_rows,
        "column_widths": column_widths,
    }


def _compute_widths(
    rows: list[list[dict[str, Any]]],
    ocr_items: list[dict] | None,
) -> list[float] | None:
    """Compute column width proportions from OCR pixel positions."""
    if not ocr_items:
        return None

    max_col = max(len(row) for row in rows) if rows else 0
    if max_col <= 1:
        return None

    # Cluster OCR x_centers into max_col groups
    x_centers = sorted([it["x"] + it.get("w", 0) / 2 for it in ocr_items])
    x_min, x_max = x_centers[0], x_centers[-1]
    if x_max - x_min < 10:
        return None

    bin_width = (x_max - x_min) / max_col
    col_widths_px = []
    for i in range(max_col):
        lo = x_min + i * bin_width
        hi = x_min + (i + 1) * bin_width
        items_in_bin = [
            it for it in ocr_items
            if lo <= (it["x"] + it.get("w", 0) / 2) < hi
        ]
        if items_in_bin:
            left = min(it["x"] for it in items_in_bin)
            right = max(it["x"] + it.get("w", 0) for it in items_in_bin)
            col_widths_px.append(max(right - left, 1))
        else:
            col_widths_px.append(bin_width)

    total = sum(col_widths_px)
    proportions = [w / total for w in col_widths_px]

    logger.info(
        "Column widths: %s",
        ", ".join(f"{p:.1%}" for p in proportions),
    )
    return proportions
