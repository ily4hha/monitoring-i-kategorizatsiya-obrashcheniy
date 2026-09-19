"""HTTP contracts across the real runtime adapters, Excel import and frontend."""
from io import BytesIO
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import create_app
from app.models import AnalyticsOverview, AppealAnalysis, ImportedRecord
from app.services.preprocessing import RAW_REQUIRED_COLUMNS


APPEAL = {
    "subject": "Ошибка авторизации",
    "description": "Не могу войти в личный кабинет, пароль не работает",
    "service": "Личный кабинет",
    "component": "Авторизация",
}
FILTERS = {
    "date_from": "2025-01-02", "date_to": "2025-01-02",
    "service": "Личный кабинет", "category": "Проблема с авторизацией",
    "priority": "Высокий", "line": "2 линия",
}


def raw_workbook(*, middle_date="2025-01-02T23:59:59"):
    rows = []
    for index in range(3):
        row = dict.fromkeys(sorted(RAW_REQUIRED_COLUMNS))
        row.update({
            "Номер запроса": f"CONTRACT-{index}",
            "Описание 2": APPEAL["description"],
            "Компонент услуги 1 уровня": APPEAL["component"],
            "Дата регистрации": f"2025-01-0{index + 1}T23:59:59",
            "Услуга": APPEAL["service"], "Вид запроса": FILTERS["category"],
            "Приоритет": FILTERS["priority"], "Кем решен (группа)": FILTERS["line"],
            "Результат работ": "Пароль сброшен, доступ восстановлен",
            "Фактическая длительность выполнения запроса (SLA)": "02:00:00",
            "Просрочен?*": "Просрочен" if index == 1 else "Не просрочен",
            "Количество уточнений": 2 if index == 1 else 0,
            "Суммарное время работы 2 линии": "01:30:00",
            "Суммарное время реакции 2 линии": "00:30:00",
            "_customer": "Сохранить исходную колонку",
        })
        rows.append(row)
    rows[1]["Дата регистрации"] = middle_date
    book = Workbook()
    sheet = book.active
    sheet.append(list(rows[0]))
    for row in rows:
        sheet.append(list(row.values()))
    output = BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        yield client


def upload(client):
    response = client.post("/api/datasets", files={"file": ("raw.xlsx", raw_workbook())})
    assert response.status_code == 201
    return response.json()


def assert_overview_contract(data):
    # No fields may silently disappear through response-model serialization.
    assert AnalyticsOverview.model_validate(data).model_dump(mode="json") == data
    assert all(data[key] == value for key, value in data["kpis"].items())
    assert data["workload"] == {
        "categories": data["category_distribution"], "lines": data["line_distribution"],
    }
    assert data["sample_size"] == data["sample_sizes"]["filtered_records"] == data["total_appeals"]
    assert data["source_size"] == data["sample_sizes"]["source_records"]


def test_startup_and_analyze_to_original_record_contract(client):
    assert client.get("/api/integrations").json() == dict.fromkeys(
        ("classification", "routing", "similarity", "analytics"), "ready",
    )
    assert client.post("/api/appeals/analyze", json=APPEAL).json()["similar_appeals"] == []
    dataset = upload(client)
    records = client.get(f"/api/datasets/{dataset['dataset_id']}/records").json()["items"]
    source = {record["_record_id"]: record for record in records}
    response = client.post("/api/appeals/analyze", json=APPEAL)
    assert response.status_code == 200
    data = response.json()
    assert AppealAnalysis.model_validate(data).model_dump(mode="json") == data
    assert len(data["similar_appeals"]) == 3
    for hit in data["similar_appeals"]:
        original = client.get(f"/api/records/{hit['record_id']}")
        assert original.status_code == 200
        record = original.json()
        assert record == source[hit["record_id"]]
        assert ImportedRecord.model_validate(record).model_dump(by_alias=True) == record
        assert record["record_id"] == record["_record_id"] == hit["record_id"]
        assert hit["appeal_number"] == record["Номер запроса"]
        assert hit["category"] == record["Вид запроса"]
        assert hit["support_line"] == record["Кем решен (группа)"]
        assert hit["resolution"] == record["Результат работ"]
        assert hit["score_kind"] == "textual_similarity"
        assert 0 < hit["score"] <= 1 and "не вероятность" in hit["score_description"]
        assert hit["explanation"]
    expected_review = (
        data["category"]["needs_manual_review"] or data["category"]["confidence"] < 0.5
        or data["routing"]["needs_manual_review"] or data["routing"]["confidence"] < 0.5
        or data["routing"]["support_line"] is None
    )
    assert data["manual_review_required"] == expected_review


def test_analytics_contract_before_import_and_with_all_filters(client):
    empty = client.get("/api/analytics/overview", params=FILTERS).json()
    assert_overview_contract(empty)
    assert empty["status"] == "no-data"
    assert empty["applied_filters"] == FILTERS
    assert not any(empty["sample_sizes"].values())
    upload(client)
    data = client.get("/api/analytics/overview", params=FILTERS).json()
    assert_overview_contract(data)
    assert data["status"] == "ready" and data["missing_columns"] == []
    assert data["applied_filters"] == FILTERS
    assert data["kpis"] == {
        "total_appeals": 1, "overdue_count": 1, "overdue_share": 1,
        "mean_sla_h": 2, "median_sla_h": 2,
        "multiline_count": 0, "high_clarifications_count": 1,
    }
    assert data["sample_sizes"] == {
        "source_records": 3, "filtered_records": 1, "sla_records": 1,
        "overdue_records": 1, "multiline_records": 1, "clarification_records": 1,
    }
    for groups in data["sla_breakdowns"].values():
        assert len(groups) == 1 and groups[0]["total_appeals"] == 1
    for dimension in ("categories", "lines"):
        risk = data["historical_sla_risk"][dimension][0]
        assert risk["status"] == "insufficient-sample"
        assert risk["minimum_group_size"] == 20
        assert risk["is_reliable"] is risk["is_elevated_historical_risk"] is False
    for field in ("service", "category", "priority", "line"):
        empty = client.get("/api/analytics/overview", params={**FILTERS, field: "Несуществующий"}).json()
        assert_overview_contract(empty)
        assert empty["status"] == "no-data" and empty["total_appeals"] == 0
        assert empty["mean_sla_h"] is empty["overdue_share"] is None


def test_partial_analytics_keeps_same_contract(client):
    book = Workbook()
    book.active.append(["Тема", "fact_sla_h"])
    book.active.append(["Ошибка", 3])
    output = BytesIO()
    book.save(output)
    assert client.post("/api/datasets", files={"file": ("partial.xlsx", output.getvalue())}).status_code == 201
    data = client.get("/api/analytics/overview").json()
    assert_overview_contract(data)
    assert data["status"] == "partial-data"
    assert data["mean_sla_h"] == 3 and data["overdue_count"] is None
    assert data["sample_sizes"]["overdue_records"] == 0


@pytest.mark.parametrize("registered_at", ["2025-01-02T23:59:59+03:00", "02.01.2025 23:59:59"])
def test_calendar_filters_handle_iso_offsets_and_local_dates(client, registered_at):
    response = client.post("/api/datasets", files={"file": (
        "dates.xlsx", raw_workbook(middle_date=registered_at),
    )})
    assert response.status_code == 201
    filtered = client.get("/api/analytics/overview", params=FILTERS)
    assert filtered.status_code == 200
    assert filtered.json()["total_appeals"] == 1
    full_range = client.get("/api/analytics/overview", params={"date_to": "9999-12-31"})
    assert full_range.status_code == 200 and full_range.json()["total_appeals"] == 3


@pytest.mark.parametrize("params,status", [
    ({"date_from": "invalid"}, 422),
    ({"date_from": "2025-01-03", "date_to": "2025-01-01"}, 400),
])
def test_analytics_rejects_invalid_date_filters(client, params, status):
    assert client.get("/api/analytics/overview", params=params).status_code == status


def test_similarity_failure_is_reported_separately_and_recovers(client, monkeypatch):
    upload(client)
    similarity = client.app.state.analysis_service.similarity
    with monkeypatch.context() as context:
        context.setattr(similarity._vectorizer, "transform", Mock(side_effect=ValueError("broken search")))
        result = client.post("/api/appeals/analyze", json=APPEAL)
        assert result.status_code == 200
        assert result.json()["similar_appeals"] == []
        status = client.get("/api/integrations").json()
        assert status["similarity"] == "unavailable:inference-error"
        assert status["routing"] == status["classification"] == status["analytics"] == "ready"
    assert client.post("/api/appeals/analyze", json=APPEAL).json()["similar_appeals"]
    assert client.get("/api/integrations").json()["similarity"] == "ready"


def test_openapi_describes_nested_responses_and_frontend_filters(client):
    spec = client.get("/openapi.json").json()
    for path, method, model in (
        ("/api/health", "get", "HealthStatus"),
        ("/api/records/{record_id}", "get", "ImportedRecord"),
        ("/api/appeals/analyze", "post", "AppealAnalysis"),
        ("/api/analytics/overview", "get", "AnalyticsOverview"),
    ):
        schema = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model}"}
    params = spec["paths"]["/api/analytics/overview"]["get"]["parameters"]
    assert {param["name"] for param in params} == set(FILTERS)
    schemas = spec["components"]["schemas"]
    assert schemas["AnalyticsOverview"]["properties"]["sla_breakdowns"] == {"$ref": "#/components/schemas/SlaBreakdowns"}
    assert schemas["SlaBreakdowns"]["properties"]["services"]["items"] == {"$ref": "#/components/schemas/SlaGroupMetrics"}
    assert schemas["ImportedRecord"]["additionalProperties"] is True
    assert set(schemas["ImportedRecord"]["required"]) == {"_record_id", "_sheet", "_source_row"}
