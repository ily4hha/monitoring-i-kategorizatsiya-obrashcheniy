from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


REAL_WORKBOOK = (
    Path(__file__).parents[1]
    / "modules"
    / "routing"
    / "data"
    / "raw"
    / "Обращения_1931.xlsx"
)


@pytest.fixture(scope="module")
def imported_client(tmp_path_factory):
    runtime = tmp_path_factory.mktemp("historical-runtime")
    with TestClient(create_app(runtime)) as client:
        with REAL_WORKBOOK.open("rb") as source:
            response = client.post(
                "/api/datasets",
                files={"file": (REAL_WORKBOOK.name, source, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        assert response.status_code == 201
        yield client, response.json()


def test_real_workbook_is_enriched_without_final_csv(imported_client) -> None:
    client, dataset = imported_client

    assert dataset["row_count"] == 1931
    assert dataset["active_sheet"] == "Sheet0"
    for column in (
        "fact_sla_h", "is_overdue", "category_original", "category_grouped",
        "resolved_line", "lines_count", "is_multiline", "clarifications_cnt",
        "is_high_clarifications", "total_work_h", "total_react_h",
    ):
        assert column in dataset["columns"]

    records = client.get(f"/api/datasets/{dataset['dataset_id']}/records?limit=3").json()
    first = records["items"][0]
    assert first["Номер запроса"] == 37811141
    assert first["ticket_id"] == "37811141"
    assert first["record_id"] == first["_record_id"]
    assert first["fact_sla_h"] == pytest.approx(60.2525)
    assert first["is_overdue"] == 1
    assert first["category_original"] == "Проблема из почты"
    assert first["category_grouped"] == "Прочее"
    assert first["resolved_line"] == "(3 линия)"
    assert first["lines_count"] == 1
    assert first["is_multiline"] == 0
    assert first["clarifications_cnt"] == 0
    assert first["is_high_clarifications"] == 0
    assert first["total_work_h"] == pytest.approx(2.8361111111111112)
    assert first["total_react_h"] == pytest.approx(57.41638888888889)


def test_real_workbook_reconstructs_missing_fact_sla(imported_client) -> None:
    client, dataset = imported_client
    match = client.get(
        f"/api/datasets/{dataset['dataset_id']}/records",
        params={"q": "38116960", "limit": 10},
    ).json()

    assert match["total"] == 1
    record = match["items"][0]
    assert record["ticket_id"] == "38116960"
    assert record["fact_sla_source"] == "reconstructed_from_lines"
    assert record["fact_sla_h"] == pytest.approx(2.2391666666666667)
    assert record["lines_count"] == 2
    assert record["fact_sla_h"] == pytest.approx(record["total_work_h"] + record["total_react_h"])


def test_real_workbook_has_full_sla_analytics_and_filters(imported_client) -> None:
    client, _ = imported_client
    overview = client.get("/api/analytics/overview").json()

    assert overview["status"] == "ready"
    assert overview["missing_columns"] == []
    assert overview["kpis"] == {
        "total_appeals": 1931,
        "overdue_count": 25,
        "overdue_share": pytest.approx(25 / 1931),
        "mean_sla_h": pytest.approx(4.02664379423442),
        "median_sla_h": pytest.approx(0.9761111111111112),
        "multiline_count": 645,
        "high_clarifications_count": 384,
    }
    assert len(overview["sla_breakdowns"]["services"]) == 4
    assert len(overview["sla_breakdowns"]["priorities"]) == 4
    assert len(overview["sla_breakdowns"]["categories"]) == 43
    assert len(overview["sla_breakdowns"]["category_groups"]) == 16
    assert len(overview["historical_sla_risk"]["categories"]) == 43
    line_four = next(row for row in overview["historical_sla_risk"]["lines"] if row["value"] == "(4 линия)")
    assert line_four["total_appeals"] == 1
    assert line_four["status"] == "insufficient-sample"

    filtered = client.get(
        "/api/analytics/overview",
        params={
            "date_from": "2025-01-01",
            "date_to": "2025-01-31",
            "priority": "(1) Наивысший",
            "line": "(3 линия)",
        },
    ).json()
    assert filtered["status"] == "ready"
    assert 0 < filtered["sample_size"] < 1931
    assert filtered["applied_filters"]["date_to"] == "2025-01-31"
    assert filtered["applied_filters"]["priority"] == "(1) Наивысший"
    assert {row["value"] for row in filtered["line_distribution"]} == {"(3 линия)"}


def test_real_workbook_analysis_opens_actual_sqlite_records(imported_client) -> None:
    client, dataset = imported_client
    assert client.get("/api/integrations").json() == dict.fromkeys(
        ("classification", "routing", "similarity", "analytics"), "ready",
    )
    record = client.get(f"/api/datasets/{dataset['dataset_id']}/records?limit=1").json()["items"][0]
    result = client.post("/api/appeals/analyze", json={
        "subject": "Проверка контракта", "description": record["Описание 2"],
        "service": record["Услуга"], "component": record["Компонент услуги 1 уровня"],
    })
    assert result.status_code == 200
    hits = result.json()["similar_appeals"]
    assert hits and any(hit["record_id"] == record["_record_id"] for hit in hits)
    for hit in hits:
        response = client.get(f"/api/records/{hit['record_id']}")
        assert response.status_code == 200
        assert response.json()["_record_id"] == hit["record_id"]
        assert str(response.json()["Номер запроса"]) == hit["appeal_number"]
