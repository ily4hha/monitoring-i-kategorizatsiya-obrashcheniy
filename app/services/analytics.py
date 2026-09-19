from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from typing import Any


SLA_COLUMN = "fact_sla_h"
OVERDUE_COLUMN = "is_overdue"
CATEGORY_COLUMN = "category_grouped"
CATEGORY_ORIGINAL_COLUMN = "category_original"
LINE_COLUMN = "resolved_line"
SERVICE_COLUMN = "service"
PRIORITY_COLUMN = "priority"
DATE_COLUMN = "reg_dt"
MULTILINE_COLUMN = "is_multiline"
HIGH_CLARIFICATIONS_COLUMN = "is_high_clarifications"
TOTAL_WORK_COLUMN = "total_work_h"
TOTAL_REACT_COLUMN = "total_react_h"
HISTORICAL_RISK_MIN_GROUP_SIZE = 20
MISSING_VALUE = "Не указано"

ANALYTICS_COLUMNS = {
    SLA_COLUMN,
    OVERDUE_COLUMN,
    CATEGORY_COLUMN,
    CATEGORY_ORIGINAL_COLUMN,
    LINE_COLUMN,
    SERVICE_COLUMN,
    PRIORITY_COLUMN,
    DATE_COLUMN,
    MULTILINE_COLUMN,
    HIGH_CLARIFICATIONS_COLUMN,
    TOTAL_WORK_COLUMN,
    TOTAL_REACT_COLUMN,
}


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _binary_flag(value: Any) -> int | None:
    number = _finite_number(value)
    return int(number) if number in {0, 1} else None


def _label(value: Any) -> str:
    if value is None:
        return MISSING_VALUE
    try:
        if math.isnan(value):
            return MISSING_VALUE
    except TypeError:
        pass
    text_value = str(value).strip()
    return text_value or MISSING_VALUE


def _distribution(records: list[Mapping[str, Any]], column: str) -> list[dict[str, Any]]:
    total = len(records)
    if not total:
        return []
    counts = Counter(_label(record.get(column)) for record in records)
    return [
        {"value": value, "count": count, "share": count / total}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _filter_value(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return None if not text_value or text_value == "Все" else text_value


def _iso_filter(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)


def _date_only(value: date | datetime | str | None) -> bool:
    return (
        isinstance(value, date) and not isinstance(value, datetime)
        or isinstance(value, str) and len(value.strip()) == 10
    )


def filter_records(
    records: Iterable[Mapping[str, Any]],
    *,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    service: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    line: str | None = None,
) -> list[Mapping[str, Any]]:
    """Filter historical records; a date-only upper bound includes that day."""
    lower = _datetime_value(date_from)
    upper = _datetime_value(date_to)
    # API filters are calendar dates in the source record. Compare dates directly
    # to include the whole day, even with an offset or the maximum valid date.
    lower_is_date = _date_only(date_from)
    upper_is_date = _date_only(date_to)
    if lower is not None and lower_is_date:
        lower = lower.date()
    if upper is not None and upper_is_date:
        upper = upper.date()
    selected_service = _filter_value(service)
    selected_category = _filter_value(category)
    selected_priority = _filter_value(priority)
    selected_line = _filter_value(line)

    filtered = []
    for record in records:
        registered_at = _datetime_value(record.get(DATE_COLUMN))
        if lower is not None and (
            registered_at is None
            or (registered_at.date() if lower_is_date else registered_at) < lower
        ):
            continue
        if upper is not None:
            if registered_at is None or (registered_at.date() if upper_is_date else registered_at) > upper:
                continue
        if selected_service and _label(record.get(SERVICE_COLUMN)) != selected_service:
            continue
        if selected_category and selected_category not in {
            _label(record.get(CATEGORY_ORIGINAL_COLUMN)),
            _label(record.get(CATEGORY_COLUMN)),
        }:
            continue
        if selected_priority and _label(record.get(PRIORITY_COLUMN)) != selected_priority:
            continue
        if selected_line and _label(record.get(LINE_COLUMN)) != selected_line:
            continue
        filtered.append(record)
    return filtered


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _group_metrics(records: list[Mapping[str, Any]], column: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        groups[_label(record.get(column))].append(record)

    results = []
    for value, rows in groups.items():
        sla_values = [
            number for row in rows
            if (number := _finite_number(row.get(SLA_COLUMN))) is not None
        ]
        overdue_flags = [
            flag for row in rows
            if (flag := _binary_flag(row.get(OVERDUE_COLUMN))) is not None
        ]
        multiline_flags = [
            flag for row in rows
            if (flag := _binary_flag(row.get(MULTILINE_COLUMN))) is not None
        ]
        clarification_flags = [
            flag for row in rows
            if (flag := _binary_flag(row.get(HIGH_CLARIFICATIONS_COLUMN))) is not None
        ]
        work_values = [
            number for row in rows
            if (number := _finite_number(row.get(TOTAL_WORK_COLUMN))) is not None
        ]
        react_values = [
            number for row in rows
            if (number := _finite_number(row.get(TOTAL_REACT_COLUMN))) is not None
        ]
        overdue_count = sum(overdue_flags) if overdue_flags else None
        results.append({
            "value": value,
            "count": len(rows),
            "total_appeals": len(rows),
            "sla_sample_size": len(sla_values),
            "overdue_sample_size": len(overdue_flags),
            "overdue_count": overdue_count,
            "overdue_share": overdue_count / len(overdue_flags) if overdue_flags else None,
            "mean_sla_h": _mean(sla_values),
            "median_sla_h": _median(sla_values),
            "multiline_count": sum(multiline_flags) if multiline_flags else None,
            "multiline_share": sum(multiline_flags) / len(multiline_flags) if multiline_flags else None,
            "high_clarifications_count": sum(clarification_flags) if clarification_flags else None,
            "mean_total_work_h": _mean(work_values),
            "mean_total_react_h": _mean(react_values),
        })
    return sorted(results, key=lambda item: (-item["total_appeals"], item["value"]))


def _historical_risk(
    records: list[Mapping[str, Any]],
    column: str,
    overall_overdue_share: float | None,
    minimum_group_size: int,
) -> list[dict[str, Any]]:
    risk_rows = []
    for group in _group_metrics(records, column):
        sample_size = group["overdue_sample_size"]
        overdue_share = group["overdue_share"]
        reliable = sample_size >= minimum_group_size
        delta = (
            overdue_share - overall_overdue_share
            if overdue_share is not None and overall_overdue_share is not None
            else None
        )
        ratio = (
            overdue_share / overall_overdue_share
            if overdue_share is not None and overall_overdue_share not in {None, 0}
            else None
        )
        elevated = bool(reliable and delta is not None and delta > 0)
        if not reliable:
            status = "insufficient-sample"
            reliability_message = (
                f"Недостаточно наблюдений: {sample_size}; "
                f"минимальный порог — {minimum_group_size}."
            )
        elif elevated:
            status = "elevated"
            reliability_message = "Оценка основана на достаточной исторической выборке."
        else:
            status = "not-elevated"
            reliability_message = "Оценка основана на достаточной исторической выборке."
        risk_rows.append({
            "value": group["value"],
            "total_appeals": group["total_appeals"],
            "overdue_sample_size": sample_size,
            "overdue_count": group["overdue_count"],
            "overdue_share": overdue_share,
            "overall_overdue_share": overall_overdue_share,
            "overdue_share_delta": delta,
            "overdue_risk_ratio": ratio,
            "minimum_group_size": minimum_group_size,
            "is_reliable": reliable,
            "is_elevated_historical_risk": elevated,
            "status": status,
            "reliability_message": reliability_message,
        })
    status_order = {"elevated": 0, "not-elevated": 1, "insufficient-sample": 2}
    return sorted(
        risk_rows,
        key=lambda item: (
            status_order[item["status"]],
            -(item["overdue_share_delta"] or 0),
            -item["total_appeals"],
            item["value"],
        ),
    )


def calculate_overview(
    records: Iterable[Mapping[str, Any]],
    columns: Iterable[str] | None = None,
    *,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    service: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    line: str | None = None,
    risk_min_group_size: int = HISTORICAL_RISK_MIN_GROUP_SIZE,
) -> dict[str, Any]:
    """Calculate filtered SLA analytics without mutating source records."""
    source_rows = list(records)
    available = set(columns) if columns is not None else {
        key for record in source_rows for key in record
    }
    missing = sorted(ANALYTICS_COLUMNS - available)
    rows = filter_records(
        source_rows,
        date_from=date_from,
        date_to=date_to,
        service=service,
        category=category,
        priority=priority,
        line=line,
    )
    total = len(rows)
    sla_values = [
        number for record in rows
        if (number := _finite_number(record.get(SLA_COLUMN))) is not None
    ]
    overdue_flags = [
        flag for record in rows
        if (flag := _binary_flag(record.get(OVERDUE_COLUMN))) is not None
    ]
    multiline_flags = [
        flag for record in rows
        if (flag := _binary_flag(record.get(MULTILINE_COLUMN))) is not None
    ]
    high_clarification_flags = [
        flag for record in rows
        if (flag := _binary_flag(record.get(HIGH_CLARIFICATIONS_COLUMN))) is not None
    ]
    overdue_count = sum(overdue_flags) if overdue_flags else None
    overdue_share = overdue_count / len(overdue_flags) if overdue_flags else None
    kpis = {
        "total_appeals": total,
        "overdue_count": overdue_count,
        "overdue_share": overdue_share,
        "mean_sla_h": _mean(sla_values),
        "median_sla_h": _median(sla_values),
        "multiline_count": sum(multiline_flags) if multiline_flags else None,
        "high_clarifications_count": sum(high_clarification_flags) if high_clarification_flags else None,
    }
    applied_filters = {
        "date_from": _iso_filter(date_from),
        "date_to": _iso_filter(date_to),
        "service": _filter_value(service),
        "category": _filter_value(category),
        "priority": _filter_value(priority),
        "line": _filter_value(line),
    }
    sample_sizes = {
        "source_records": len(source_rows),
        "filtered_records": total,
        "sla_records": len(sla_values),
        "overdue_records": len(overdue_flags),
        "multiline_records": len(multiline_flags),
        "clarification_records": len(high_clarification_flags),
    }
    category_distribution = _distribution(rows, CATEGORY_COLUMN) if CATEGORY_COLUMN in available else []
    line_distribution = _distribution(rows, LINE_COLUMN) if LINE_COLUMN in available else []

    result: dict[str, Any] = {
        "status": "ready" if not missing else "partial-data",
        **kpis,
        "kpis": kpis,
        "category_distribution": category_distribution,
        "line_distribution": line_distribution,
        "workload": {
            "categories": category_distribution,
            "lines": line_distribution,
        },
        "sla_breakdowns": {
            "services": _group_metrics(rows, SERVICE_COLUMN) if SERVICE_COLUMN in available else [],
            "categories": _group_metrics(rows, CATEGORY_ORIGINAL_COLUMN) if CATEGORY_ORIGINAL_COLUMN in available else [],
            "category_groups": _group_metrics(rows, CATEGORY_COLUMN) if CATEGORY_COLUMN in available else [],
            "priorities": _group_metrics(rows, PRIORITY_COLUMN) if PRIORITY_COLUMN in available else [],
            "lines": _group_metrics(rows, LINE_COLUMN) if LINE_COLUMN in available else [],
        },
        "historical_sla_risk": {
            "definition": (
                "Историческая доля просроченных обращений в группе относительно "
                "общей доли в отфильтрованной выборке. Это не прогноз."
            ),
            "minimum_group_size": risk_min_group_size,
            "overall_overdue_share": overdue_share,
            "categories": _historical_risk(
                rows, CATEGORY_ORIGINAL_COLUMN, overdue_share, risk_min_group_size
            ) if CATEGORY_ORIGINAL_COLUMN in available and OVERDUE_COLUMN in available else [],
            "lines": _historical_risk(
                rows, LINE_COLUMN, overdue_share, risk_min_group_size
            ) if LINE_COLUMN in available and OVERDUE_COLUMN in available else [],
        },
        "applied_filters": applied_filters,
        "sample_sizes": sample_sizes,
        "source_size": len(source_rows),
        "sample_size": total,
        "missing_columns": missing,
        "message": "Аналитика рассчитана по загруженному датасету.",
    }
    if missing:
        result["message"] = "Недостаточно колонок для всех SLA-метрик: " + ", ".join(missing)
    if not total:
        result.update(status="no-data", message="По заданным фильтрам обращений нет.")
    return result
