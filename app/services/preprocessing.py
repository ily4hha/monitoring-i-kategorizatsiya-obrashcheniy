"""Runtime enrichment for the historical appeals workbook.

The source columns are kept intact.  Normalized aliases and analytical fields
mirror the transformations used by the historical workbook analysis.  No
runtime artifact such as a precomputed CSV is required.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd


OTHER_CATEGORY = "Прочее"
TOP_CATEGORIES_COUNT = 15

TICKET_COLUMN = "Номер запроса"
REGISTRATION_COLUMN = "Дата регистрации"
SERVICE_COLUMN = "Услуга"
CATEGORY_COLUMN = "Вид запроса"
PRIORITY_COLUMN = "Приоритет"
FACT_SLA_COLUMN = "Фактическая длительность выполнения запроса (SLA)"
OVERDUE_COLUMN = "Просрочен?*"
RESOLVED_LINE_COLUMN = "Кем решен (группа)"
CLARIFICATIONS_COLUMN = "Количество уточнений"

LINE_SOURCE_COLUMNS = {
    f"line_{line}_react_h": f"Суммарное время реакции {line} линии"
    for line in range(1, 5)
} | {
    f"line_{line}_work_h": f"Суммарное время работы {line} линии"
    for line in range(1, 5)
}

RAW_REQUIRED_COLUMNS = {
    TICKET_COLUMN,
    REGISTRATION_COLUMN,
    SERVICE_COLUMN,
    CATEGORY_COLUMN,
    PRIORITY_COLUMN,
    FACT_SLA_COLUMN,
    OVERDUE_COLUMN,
    RESOLVED_LINE_COLUMN,
    CLARIFICATIONS_COLUMN,
    *LINE_SOURCE_COLUMNS.values(),
}

# ``record_id`` is metadata and intentionally does not become a displayed
# dataset column.  It is added to every enriched payload next to ``_record_id``.
ENRICHED_COLUMNS = (
    "ticket_id",
    "reg_dt",
    "service",
    "category_original",
    "category_grouped",
    "priority",
    "resolved_line",
    "fact_sla_h",
    "fact_sla_source",
    "is_overdue",
    "lines_count",
    "is_multiline",
    "clarifications_cnt",
    "is_high_clarifications",
    "total_work_h",
    "total_react_h",
)


def supports_historical_preprocessing(columns: list[str] | tuple[str, ...]) -> bool:
    """Return whether a sheet has the complete raw schema used by the notebook."""
    return RAW_REQUIRED_COLUMNS.issubset(columns)


def needs_historical_preprocessing(columns: list[str] | tuple[str, ...]) -> bool:
    """Return whether a recognized raw sheet has not yet been enriched."""
    return supports_historical_preprocessing(columns) and not set(ENRICHED_COLUMNS).issubset(columns)


def enriched_columns(columns: list[str]) -> list[str]:
    """Append analytical columns without dropping or reordering source columns."""
    return [*columns, *(column for column in ENRICHED_COLUMNS if column not in columns)]


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text_value = re.sub(r"\s+", " ", str(value)).strip()
    return text_value or None


def category_for_frequency(record: Mapping[str, Any]) -> str | None:
    category = clean_text(record.get(CATEGORY_COLUMN))
    return category if category and category != OTHER_CATEGORY else None


def select_top_categories(counts: Mapping[str, int]) -> frozenset[str]:
    """Choose the dataset-level top 15; stable order resolves equal counts."""
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    return frozenset(category for category, _ in ranked[:TOP_CATEGORIES_COUNT])


def parse_duration_to_hours(value: Any) -> float | None:
    """Convert H:MM[:SS] or an Excel time value to hours without filling gaps."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, timedelta):
        return value.total_seconds() / 3600
    if isinstance(value, time):
        return value.hour + value.minute / 60 + value.second / 3600 + value.microsecond / 3_600_000_000
    if not isinstance(value, str):
        return None

    text_value = value.strip()
    if not text_value:
        return None
    negative = text_value.startswith("-")
    if negative:
        text_value = text_value[1:]
    parts = text_value.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        numbers = [float(part) for part in parts]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in numbers):
        return None
    hours = numbers[0] + numbers[1] / 60
    if len(numbers) == 3:
        hours += numbers[2] / 3600
    return -hours if negative else hours


def _datetime_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, time()).isoformat()
    if isinstance(value, str):
        try:
            # dayfirst applies to local dates, not unambiguous ISO timestamps.
            return datetime.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    parsed = pd.to_datetime(value, errors="coerce", format="mixed", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().isoformat()


def _clarifications_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _overdue_flag(value: Any) -> int | None:
    label = clean_text(value)
    if label is None:
        return None
    normalized = label.casefold()
    if normalized == "просрочен":
        return 1
    if normalized == "не просрочен":
        return 0
    return None


def preprocess_record(record: Mapping[str, Any], top_categories: frozenset[str]) -> dict[str, Any]:
    """Return one source record enriched with deterministic analytical fields."""
    enriched = dict(record)
    category = clean_text(record.get(CATEGORY_COLUMN))
    line_values = {
        target: parse_duration_to_hours(record.get(source)) or 0.0
        for target, source in LINE_SOURCE_COLUMNS.items()
    }
    react_values = [line_values[f"line_{line}_react_h"] for line in range(1, 5)]
    work_values = [line_values[f"line_{line}_work_h"] for line in range(1, 5)]
    line_sum = sum(react_values) + sum(work_values)
    source_fact_sla = parse_duration_to_hours(record.get(FACT_SLA_COLUMN))
    if source_fact_sla is not None:
        fact_sla_h = source_fact_sla
        fact_sla_source = "source"
    elif line_sum > 0:
        fact_sla_h = line_sum
        fact_sla_source = "reconstructed_from_lines"
    else:
        fact_sla_h = None
        fact_sla_source = "missing"

    clarifications = _clarifications_count(record.get(CLARIFICATIONS_COLUMN))
    lines_count = sum(
        react_values[index] > 0 or work_values[index] > 0
        for index in range(4)
    )
    enriched.update(
        record_id=record.get("_record_id"),
        ticket_id=clean_text(record.get(TICKET_COLUMN)),
        reg_dt=_datetime_iso(record.get(REGISTRATION_COLUMN)),
        service=clean_text(record.get(SERVICE_COLUMN)),
        category_original=category,
        category_grouped=category if category in top_categories else OTHER_CATEGORY,
        priority=clean_text(record.get(PRIORITY_COLUMN)),
        resolved_line=clean_text(record.get(RESOLVED_LINE_COLUMN)),
        fact_sla_h=fact_sla_h,
        fact_sla_source=fact_sla_source,
        lines_count=lines_count,
        is_multiline=int(lines_count > 1),
        clarifications_cnt=clarifications,
        is_high_clarifications=None if clarifications is None else int(clarifications >= 2),
        total_work_h=sum(work_values),
        total_react_h=sum(react_values),
        is_overdue=_overdue_flag(record.get(OVERDUE_COLUMN)),
    )
    return enriched
