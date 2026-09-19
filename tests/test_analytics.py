from __future__ import annotations

from pathlib import Path
from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.analytics import ANALYTICS_COLUMNS, MISSING_VALUE, calculate_overview


FIXTURE = Path(__file__).parent / "fixtures" / "sla_analytics.csv"


@pytest.fixture()
def sla_frame() -> pd.DataFrame:
    return pd.read_csv(FIXTURE, dtype={"ticket_id": str}).where(pd.notna, None)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path / "runtime"))


def workbook_from_frame(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False)
    return buffer.getvalue()


def test_sla_formulas_and_distributions(sla_frame) -> None:
    overview = calculate_overview(sla_frame.to_dict("records"), sla_frame.columns)

    assert overview["status"] == "partial-data"
    assert overview["total_appeals"] == 4
    assert overview["overdue_count"] == 2
    assert overview["overdue_share"] == pytest.approx(0.5)
    assert overview["mean_sla_h"] == pytest.approx(4)
    assert overview["median_sla_h"] == pytest.approx(3)
    assert overview["category_distribution"] == [
        {"value": "Категория A", "count": 2, "share": 0.5},
        {"value": "Категория B", "count": 1, "share": 0.25},
        {"value": MISSING_VALUE, "count": 1, "share": 0.25},
    ]
    assert overview["line_distribution"][0] == {"value": "2 линия", "count": 2, "share": 0.5}
    assert sum(item["count"] for item in overview["line_distribution"]) == 4


def test_missing_columns_return_partial_metrics() -> None:
    overview = calculate_overview([{"fact_sla_h": None}, {"fact_sla_h": "2.5"}], ["fact_sla_h"])

    assert overview["status"] == "partial-data"
    assert overview["total_appeals"] == 2
    assert overview["overdue_count"] is None
    assert overview["overdue_share"] is None
    assert overview["mean_sla_h"] == overview["median_sla_h"] == 2.5
    assert overview["category_distribution"] == overview["line_distribution"] == []
    assert overview["missing_columns"] == sorted(ANALYTICS_COLUMNS - {"fact_sla_h"})


def test_empty_records_are_handled() -> None:
    overview = calculate_overview([], [])
    assert overview["status"] == "no-data"
    assert overview["total_appeals"] == 0
    assert overview["mean_sla_h"] is None


def test_analytics_api_uses_uploaded_dataset(client, sla_frame) -> None:
    response = client.post(
        "/api/datasets",
        files={"file": ("sla.xlsx", workbook_from_frame(sla_frame))},
    )
    assert response.status_code == 201

    overview = client.get("/api/analytics/overview")
    assert overview.status_code == 200
    assert overview.json()["total_appeals"] == 4
    assert overview.json()["overdue_count"] == 2
    assert overview.json()["mean_sla_h"] == pytest.approx(4)


def test_analytics_api_preserves_legacy_no_data_fields(client) -> None:
    data = client.get("/api/analytics/overview").json()
    expected_legacy_fields = {
        "status": "no-data",
        "total_appeals": 0,
        "overdue_share": None,
        "message": "Загрузите Excel, чтобы построить аналитику.",
    }
    assert {key: data[key] for key in expected_legacy_fields} == expected_legacy_fields


def test_integrations_reports_analytics_ready(client) -> None:
    assert client.get("/api/integrations").json()["analytics"] == "ready"


def _complete_record(**overrides):
    record = {
        "ticket_id": "1",
        "reg_dt": "2025-01-01T12:00:00",
        "service": "Услуга A",
        "category_original": "Категория A",
        "category_grouped": "Категория A",
        "priority": "Высокий",
        "resolved_line": "1 линия",
        "fact_sla_h": 2,
        "is_overdue": 0,
        "is_multiline": 0,
        "is_high_clarifications": 0,
        "total_work_h": 1,
        "total_react_h": 1,
    }
    record.update(overrides)
    return record


def test_filters_and_sla_breakdowns() -> None:
    records = [
        _complete_record(ticket_id="1", reg_dt="2025-01-01T23:59:59", fact_sla_h=2),
        _complete_record(ticket_id="2", reg_dt="2025-01-02T00:00:00", fact_sla_h=4, is_overdue=1),
        _complete_record(ticket_id="3", reg_dt="2025-01-02T10:00:00", service="Услуга B", fact_sla_h=8),
        _complete_record(ticket_id="4", reg_dt="2025-01-03T10:00:00", priority="Низкий", resolved_line="2 линия"),
    ]

    overview = calculate_overview(
        records,
        ANALYTICS_COLUMNS,
        date_from="2025-01-02",
        date_to="2025-01-02",
        service="Услуга A",
        category="Категория A",
        priority="Высокий",
        line="1 линия",
    )

    assert overview["status"] == "ready"
    assert overview["kpis"]["total_appeals"] == 1
    assert overview["kpis"]["mean_sla_h"] == 4
    assert overview["applied_filters"]["date_to"] == "2025-01-02"
    assert overview["sample_sizes"] == {
        "source_records": 4,
        "filtered_records": 1,
        "sla_records": 1,
        "overdue_records": 1,
        "multiline_records": 1,
        "clarification_records": 1,
    }
    service_row = overview["sla_breakdowns"]["services"][0]
    assert service_row["value"] == "Услуга A"
    assert service_row["overdue_share"] == 1
    assert service_row["median_sla_h"] == 4


def test_historical_risk_marks_elevated_and_small_groups() -> None:
    records = [
        *[_complete_record(ticket_id=f"a{i}", category_original="A", category_grouped="A", is_overdue=int(i < 2)) for i in range(3)],
        *[_complete_record(ticket_id=f"b{i}", category_original="B", category_grouped="B", is_overdue=0) for i in range(3)],
        _complete_record(ticket_id="tiny", category_original="Tiny", category_grouped="Tiny", resolved_line="4 линия", is_overdue=1),
    ]
    overview = calculate_overview(records, ANALYTICS_COLUMNS, risk_min_group_size=3)
    risks = {row["value"]: row for row in overview["historical_sla_risk"]["categories"]}

    assert "не прогноз" in overview["historical_sla_risk"]["definition"].casefold()
    assert risks["A"]["status"] == "elevated"
    assert risks["A"]["is_reliable"] is True
    assert risks["A"]["overdue_share"] == pytest.approx(2 / 3)
    assert risks["Tiny"]["status"] == "insufficient-sample"
    assert risks["Tiny"]["is_reliable"] is False
    assert risks["Tiny"]["is_elevated_historical_risk"] is False
    assert "Недостаточно наблюдений" in risks["Tiny"]["reliability_message"]


def test_analytics_api_validates_date_range(client) -> None:
    response = client.get("/api/analytics/overview?date_from=2025-02-01&date_to=2025-01-01")
    assert response.status_code == 400
