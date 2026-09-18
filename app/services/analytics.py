from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any, Iterable, Mapping


SLA_COLUMN = "fact_sla_h"
OVERDUE_COLUMN = "is_overdue"
CATEGORY_COLUMN = "category_grouped"
LINE_COLUMN = "resolved_line"
ANALYTICS_COLUMNS = {SLA_COLUMN, OVERDUE_COLUMN, CATEGORY_COLUMN, LINE_COLUMN}
MISSING_VALUE = "Не указано"


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _distribution(records: list[Mapping[str, Any]], column: str) -> list[dict[str, Any]]:
    total = len(records)
    if not total:
        return []

    def label(value: Any) -> str:
        if value is None:
            return MISSING_VALUE
        try:
            if math.isnan(value):
                return MISSING_VALUE
        except TypeError:
            pass
        text = str(value).strip()
        return text or MISSING_VALUE

    counts = Counter(label(record.get(column)) for record in records)
    return [
        {"value": value, "count": count, "share": count / total}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def calculate_overview(
    records: Iterable[Mapping[str, Any]],
    columns: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Calculate SLA metrics without mutating or filling source records."""
    rows = list(records)
    available = set(columns) if columns is not None else {
        key for record in rows for key in record
    }
    missing = sorted(ANALYTICS_COLUMNS - available)
    total = len(rows)

    result: dict[str, Any] = {
        "status": "ready" if not missing else "partial-data",
        "total_appeals": total,
        "overdue_count": None,
        "overdue_share": None,
        "mean_sla_h": None,
        "median_sla_h": None,
        "category_distribution": [],
        "line_distribution": [],
        "missing_columns": missing,
        "message": "Аналитика рассчитана по загруженному датасету.",
    }
    if missing:
        result["message"] = "Недостаточно колонок для всех SLA-метрик: " + ", ".join(missing)
    if not total:
        result.update(status="no-data", message="В датасете нет обращений.")
        return result

    if OVERDUE_COLUMN in available:
        flags = [_finite_number(record.get(OVERDUE_COLUMN)) for record in rows]
        overdue = sum(1 for flag in flags if flag == 1)
        result["overdue_count"] = overdue
        result["overdue_share"] = overdue / total

    if SLA_COLUMN in available:
        sla_values = [
            number for record in rows
            if (number := _finite_number(record.get(SLA_COLUMN))) is not None
        ]
        if sla_values:
            result["mean_sla_h"] = statistics.fmean(sla_values)
            result["median_sla_h"] = statistics.median(sla_values)

    if CATEGORY_COLUMN in available:
        result["category_distribution"] = _distribution(rows, CATEGORY_COLUMN)
    if LINE_COLUMN in available:
        result["line_distribution"] = _distribution(rows, LINE_COLUMN)
    return result
