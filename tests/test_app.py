from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AppealInput, CategoryPrediction, RoutingPrediction
from app.services.integrations import AnalysisService


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path / "runtime"))


def make_workbook() -> bytes:
    buffer = BytesIO()
    frame = pd.DataFrame(
        [
            {"Тема": "Не приходит письмо", "Приоритет": "Высокий"},
            {"Тема": "Ошибка авторизации", "Приоритет": "Средний"},
        ]
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Обращения", index=False)
    return buffer.getvalue()


def workbook_from_frame(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False)
    return buffer.getvalue()


def test_health(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_and_read_records(client) -> None:
    response = client.post(
        "/api/datasets",
        files={
            "file": (
                "appeals.xlsx",
                make_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 201
    dataset = response.json()
    assert dataset["row_count"] == 2
    assert dataset["active_sheet"] == "Обращения"

    records = client.get(f"/api/datasets/{dataset['dataset_id']}/records").json()
    assert records["total"] == 2
    assert records["items"][0]["Тема"] == "Не приходит письмо"


def test_pending_integrations_require_manual_review(client) -> None:
    response = client.post(
        "/api/appeals/analyze",
        json={"subject": "Проблема", "description": "Не удаётся выполнить действие"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["manual_review_required"] is True
    assert result["category"]["confidence"] == 0


@pytest.mark.parametrize("name,content", [("empty.xlsx", b""), ("broken.xlsx", b"not an excel")])
def test_rejects_empty_or_broken_file(client, name, content) -> None:
    response = client.post("/api/datasets", files={"file": (name, content)})
    assert response.status_code == 400
    assert client.get("/api/datasets/current").json() is None


def test_rejects_headers_only_and_duplicate_trimmed_headers(client) -> None:
    headers_only = client.post("/api/datasets", files={"file": ("headers.xlsx", workbook_from_frame(pd.DataFrame(columns=["A"])))})
    assert headers_only.status_code == 400
    duplicate = pd.DataFrame([["x", "y"]], columns=["A", " A "])
    response = client.post("/api/datasets", files={"file": ("duplicate.xlsx", workbook_from_frame(duplicate))})
    assert response.status_code == 400
    assert "повторяющиеся" in response.json()["detail"]


def test_preserves_text_values_and_search_is_casefolded_and_literal(client) -> None:
    frame = pd.DataFrame([{"Код": "00123", "Текст": "НА_ТЕСТ % NA NULL"}, {"Код": "00456", "Текст": "Привет Мир"}])
    dataset = client.post("/api/datasets", files={"file": ("values.xlsx", workbook_from_frame(frame))}).json()
    records = client.get(f"/api/datasets/{dataset['dataset_id']}/records?q=привет").json()
    assert records["total"] == 1
    original = client.get(f"/api/records/{records['items'][0]['_record_id']}").json()
    assert original["Код"] == "00456"
    all_records = client.get(f"/api/datasets/{dataset['dataset_id']}/records?q=%").json()
    assert all_records["total"] == 1
    first = client.get(f"/api/datasets/{dataset['dataset_id']}/records").json()["items"][0]
    assert first["Код"] == "00123" and "NA NULL" in first["Текст"]
    too_far = client.get(f"/api/datasets/{dataset['dataset_id']}/records?offset=100001")
    assert too_far.status_code == 400
    underscore = client.get(f"/api/datasets/{dataset['dataset_id']}/records?q=_")
    assert underscore.json()["total"] == 1


def test_skips_empty_additional_sheet(client) -> None:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame([{"A": "value"}]).to_excel(writer, sheet_name="Data", index=False)
        pd.DataFrame().to_excel(writer, sheet_name="Empty", index=False)
    response = client.post("/api/datasets", files={"file": ("with-empty.xlsx", buffer.getvalue())})
    assert response.status_code == 201
    assert [sheet["name"] for sheet in response.json()["sheets"]] == ["Data"]


def test_reserved_columns_and_sparse_sheet_are_rejected(client) -> None:
    reserved = pd.DataFrame([{ "_record_id": "spoof" }])
    assert client.post("/api/datasets", files={"file": ("reserved.xlsx", workbook_from_frame(reserved))}).status_code == 400
    sparse = pd.DataFrame([["x"] + [""] * 199] * 501, columns=[f"c{i}" for i in range(200)])
    response = client.post("/api/datasets", files={"file": ("sparse.xlsx", workbook_from_frame(sparse))})
    assert response.status_code == 400


def test_repeated_import_is_idempotent_and_uses_test_runtime_only(client, tmp_path) -> None:
    content = make_workbook()
    first = client.post("/api/datasets", files={"file": ("one.xlsx", content)}).json()
    second = client.post("/api/datasets", files={"file": ("two.xlsx", content)}).json()
    assert first["dataset_id"] == second["dataset_id"]
    assert second["filename"] == "one.xlsx"
    assert client.get("/api/datasets/current").json()["dataset_id"] == first["dataset_id"]
    assert client.app.state.dataset_store.db_path == tmp_path / "runtime" / "app.db"


def test_low_confidence_classifier_requires_manual_review() -> None:
    class LowConfidenceClassifier:
        def predict(self, appeal):
            return CategoryPrediction(category="Тест", confidence=0.2, explanation="Тест", needs_manual_review=False)

    class CertainRouter:
        def recommend(self, appeal, category):
            return RoutingPrediction(support_line="Первая", confidence=1, explanation="Тест")

    result = AnalysisService(classifier=LowConfidenceClassifier(), router=CertainRouter()).analyze(AppealInput(subject="x", description="y"))
    assert result.manual_review_required is True


def test_analysis_adapter_error_returns_service_unavailable(tmp_path) -> None:
    class BrokenAnalysis:
        @staticmethod
        def analyze(appeal):
            raise RuntimeError("adapter failed")

        @staticmethod
        def status():
            return AnalysisService().status()

    with TestClient(create_app(tmp_path / "runtime", analysis=BrokenAnalysis())) as client:
        response = client.post(
            "/api/appeals/analyze",
            json={"subject": "Проблема", "description": "Описание"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Сервис анализа временно недоступен"}
