from __future__ import annotations

from pathlib import Path
from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.analytics import MISSING_VALUE, calculate_overview


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

    assert overview["status"] == "ready"
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
    assert overview["missing_columns"] == ["category_grouped", "is_overdue", "resolved_line"]


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
    assert client.get("/api/analytics/overview").json() == {
        "status": "no-data",
        "total_appeals": 0,
        "overdue_share": None,
        "message": "Загрузите Excel, чтобы построить аналитику.",
    }


def test_integrations_reports_analytics_ready(client) -> None:
    assert client.get("/api/integrations").json()["analytics"] == "ready"
