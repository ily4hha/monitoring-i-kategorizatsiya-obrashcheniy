from io import BytesIO
from unittest.mock import Mock

import joblib
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.exceptions import InconsistentVersionWarning

from app.main import create_app
from app.models import AppealInput, CategoryPrediction, RoutingPrediction, SimilarAppeal
from app.services import routing
from app.services.integrations import AnalysisService


APPEAL = {"subject": "Ошибка авторизации", "description": "Не могу войти в личный кабинет, пароль не работает"}


def history_workbook(rows: list[dict] | None = None) -> bytes:
    rows = rows or [
        {
            "Номер запроса": "INC-100",
            "Описание 2": "Ошибка авторизации в личном кабинете, пароль не работает",
            "Услуга": "Личный кабинет",
            "Компонент услуги 1 уровня": "Авторизация",
            "Вид запроса": "Инцидент",
            "Кем решен (группа)": "2 линия",
            "Результат работ": "Пароль сброшен, доступ восстановлен",
        },
        {
            "Номер запроса": "INC-200",
            "Описание 2": "Не печатается почтовая накладная",
            "Услуга": "Партионный приём",
            "Компонент услуги 1 уровня": "Печать",
            "Вид запроса": "Проблема",
            "Кем решен (группа)": "1 линия",
            "Результат работ": "Перезапущена очередь печати",
        },
    ]
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Обращения", index=False)
    return output.getvalue()


def upload_history(client, content: bytes, name: str = "history.xlsx") -> dict:
    response = client.post("/api/datasets", files={"file": (name, content)})
    assert response.status_code == 201
    return response.json()


def test_real_model_loaded_once_and_similarity_uses_uploaded_dataset(tmp_path, monkeypatch):
    classifier = Mock(status="model-missing")
    classifier.predict.return_value = CategoryPrediction(
        category=None, confidence=0, explanation="Модель отсутствует.", needs_manual_review=True,
    )
    monkeypatch.setattr("app.services.integrations.ClassifierAdapter", lambda: classifier)
    original = joblib.load
    loads = Mock(wraps=original)
    monkeypatch.setattr(joblib, "load", loads)
    app = create_app(tmp_path / "runtime")
    loads.assert_not_called()
    with TestClient(app) as client:
        loads.assert_called_once_with(routing.MODEL_PATH)
        service = app.state.analysis_service
        model = service.router.runtime.assistant
        assert model is not None
        statuses = client.get("/api/integrations").json()
        assert statuses == dict(classification="model-missing", routing="ready", similarity="ready", analytics="ready")
        expected = model.recommend_line(routing.appeal_text(AppealInput(**APPEAL)))
        assert expected["line"] is not None
        assert client.post("/api/appeals/analyze", json=APPEAL).json()["similar_appeals"] == []
        dataset = upload_history(client, history_workbook())
        active_ids = {
            item["_record_id"]
            for item in client.get(f"/api/datasets/{dataset['dataset_id']}/records").json()["items"]
        }
        for _ in range(3):
            response = client.post("/api/appeals/analyze", json={
                **APPEAL, "service": "Личный кабинет", "component": "Авторизация",
            })
            assert response.status_code == 200
            result = response.json()
            assert result["routing"]["support_line"] == expected["line"]
            assert result["routing"]["confidence"] == pytest.approx(expected["confidence"])
            assert result["routing"]["needs_manual_review"] == expected["needs_review"]
            assert expected["explanation"] in result["routing"]["explanation"]
            assert result["similar_appeals"]
            hit = result["similar_appeals"][0]
            assert hit["record_id"] in active_ids
            assert hit["appeal_number"] == "INC-100"
            assert hit["score_kind"] == "textual_similarity"
            assert "не вероятность" in hit["score_description"]
            assert hit["category"] == "Инцидент"
            assert hit["support_line"] == "2 линия"
            assert hit["resolution"] == "Пароль сброшен, доступ восстановлен"
            assert "Совпали значимые термины" in hit["explanation"]
            assert client.get(f"/api/records/{hit['record_id']}").status_code == 200
        assert isinstance(service.router.recommend(AppealInput(**APPEAL), None), RoutingPrediction)
        for limit in [1, 5, 100]:
            hits = service.similarity.search(AppealInput(**APPEAL), limit)
            assert 0 < len(hits) <= min(limit, 5)
            assert all(isinstance(hit, SimilarAppeal) for hit in hits)
        high_priority = service.similarity.search(AppealInput(**APPEAL, priority="Высокий"))
        low_priority = service.similarity.search(AppealInput(**APPEAL, priority="Низкий"))
        assert high_priority == low_priority
        assert service.similarity.search(AppealInput(**APPEAL), 0) == []
        assert service.similarity.search(AppealInput(**APPEAL), -1) == []
        unknown = client.post("/api/appeals/analyze", json={"subject": "zzzxqvv", "description": "zzzxqvv"}).json()
        assert unknown["routing"]["support_line"] is None
        assert unknown["routing"]["confidence"] == 0
        assert unknown["similar_appeals"] == []
        assert unknown["manual_review_required"] is True
        service.router.runtime.start()
        # Uploads cannot become model artifacts or trigger another load.
        assert client.post("/api/datasets", files={"file": ("assistant.joblib", b"untrusted")}).status_code == 400
        loads.assert_called_once_with(routing.MODEL_PATH)


@pytest.mark.parametrize("artifact,reason", [
    ("missing", "model-missing"), ("corrupt", "model-incompatible"),
    ("wrong-type", "model-incompatible"), ("broken-classifier", "model-incompatible"),
])
def test_artifact_fallback(tmp_path, monkeypatch, artifact, reason):
    path = tmp_path / "assistant.joblib"
    if artifact == "corrupt":
        path.write_bytes(b"invalid joblib")
    elif artifact == "wrong-type":
        joblib.dump({"not": "an assistant"}, path)
    elif artifact == "broken-classifier":
        assistant = routing.load_assistant()
        assistant.classifier = None
        joblib.dump(assistant, path)
    monkeypatch.setattr(routing, "MODEL_PATH", path)
    loader = Mock(wraps=routing.load_assistant)
    monkeypatch.setattr(routing, "load_assistant", loader)
    with TestClient(create_app(tmp_path / "runtime")) as client:
        for _ in range(2):
            result = client.post("/api/appeals/analyze", json=APPEAL)
            assert result.status_code == 200
            data = result.json()
            assert data["routing"]["support_line"] is None
            assert data["routing"]["confidence"] == 0
            assert "ручной разбор" in data["routing"]["explanation"]
            assert data["similar_appeals"] == []
            assert data["manual_review_required"] is True
        status = client.get("/api/integrations").json()
        assert status["routing"] == f"unavailable:{reason}"
        assert status["similarity"] == "ready"
        assert client.get("/api/health").status_code == 200
    loader.assert_called_once_with()


def test_sklearn_version_warning_is_incompatible(tmp_path, monkeypatch):
    import warnings

    def incompatible(path):
        warnings.warn(InconsistentVersionWarning(
            estimator_name="TfidfVectorizer", current_sklearn_version="1.7.2", original_sklearn_version="0.1",
        ))
    monkeypatch.setattr(joblib, "load", incompatible)
    with TestClient(create_app(tmp_path / "runtime")) as client:
        assert client.get("/api/integrations").json()["routing"] == "unavailable:model-incompatible"


def test_routing_inference_failure_degrades_without_reloading(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        runtime = client.app.state.analysis_service.router.runtime
        assert runtime.status == "ready"
        monkeypatch.setattr(runtime.assistant, "recommend_line", Mock(side_effect=ValueError("broken inference")))
        response = client.post("/api/appeals/analyze", json=APPEAL)
        assert response.status_code == 200
        assert response.json()["similar_appeals"] == []
        assert client.get("/api/integrations").json()["routing"] == "unavailable:inference-error"
        monkeypatch.setattr(joblib, "load", Mock(side_effect=AssertionError("unexpected reload")))
        result = client.post("/api/appeals/analyze", json=APPEAL).json()
        assert result["routing"]["support_line"] is None
        assert result["routing"]["confidence"] == 0
        assert result["manual_review_required"] is True


def test_similarity_index_rebuilds_for_reupload_and_returns_only_active_ids(tmp_path):
    first_content = history_workbook()
    second_content = history_workbook([{
        "Номер запроса": "MAIL-1",
        "Описание 2": "Письмо застряло в очереди и не доставляется",
        "Услуга": "Корпоративная почта",
        "Компонент услуги 1 уровня": "Доставка",
        "Вид запроса": "Инцидент почты",
        "Кем решен (группа)": "3 линия",
        "Результат работ": "Очередь очищена, письмо доставлено",
    }])
    with TestClient(create_app(tmp_path / "runtime")) as client:
        first = upload_history(client, first_content, "first.xlsx")
        first_ids = {
            item["_record_id"]
            for item in client.get(f"/api/datasets/{first['dataset_id']}/records").json()["items"]
        }
        second = upload_history(client, second_content, "second.xlsx")
        second_ids = {
            item["_record_id"]
            for item in client.get(f"/api/datasets/{second['dataset_id']}/records").json()["items"]
        }
        response = client.post("/api/appeals/analyze", json={
            "subject": "Проблема доставки письма",
            "description": "Письмо застряло в очереди",
            "service": "Корпоративная почта",
            "component": "Доставка",
        }).json()
        assert response["similar_appeals"]
        assert {hit["record_id"] for hit in response["similar_appeals"]} <= second_ids
        assert not ({hit["record_id"] for hit in response["similar_appeals"]} & first_ids)

        # Re-uploading identical bytes reactivates the older dataset and rebuilds the index.
        repeated = upload_history(client, first_content, "renamed.xlsx")
        assert repeated["dataset_id"] == first["dataset_id"]
        response = client.post("/api/appeals/analyze", json=APPEAL).json()
        assert response["similar_appeals"]
        assert {hit["record_id"] for hit in response["similar_appeals"]} <= first_ids
        assert all(client.get(f"/api/records/{hit['record_id']}").status_code == 200
                   for hit in response["similar_appeals"])

    # The active dataset is indexed again on application startup.
    with TestClient(create_app(tmp_path / "runtime")) as restarted:
        hits = restarted.post("/api/appeals/analyze", json=APPEAL).json()["similar_appeals"]
        assert hits and {hit["record_id"] for hit in hits} <= first_ids


def test_similarity_empty_text_no_dataset_no_results_and_limit(tmp_path):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        service = client.app.state.analysis_service.similarity
        assert service.search(AppealInput(
            subject=" ", description=" ", service="Личный кабинет", component="Авторизация",
        )) == []
        assert client.post("/api/appeals/analyze", json=APPEAL).json()["similar_appeals"] == []

        rows = [
            {
                "Номер запроса": f"INC-{index}",
                "Описание 2": "общая ошибка авторизации пароль кабинет",
                "Услуга": "Личный кабинет",
                "Компонент услуги 1 уровня": "Авторизация",
                "Вид запроса": "Инцидент",
                "Кем решен (группа)": "1 линия",
                "Результат работ": "Доступ восстановлен",
            }
            for index in range(7)
        ]
        upload_history(client, history_workbook(rows))
        assert len(service.search(AppealInput(**APPEAL), limit=100)) == 5
        no_results = client.post("/api/appeals/analyze", json={
            "subject": "zzzxqvv", "description": "zzzxqvv",
        }).json()
        assert no_results["similar_appeals"] == []


def test_model_review_flag_preserved_with_confident_classifier():
    runtime = routing.RoutingRuntime()
    runtime.assistant = Mock()
    runtime.assistant.recommend_line.return_value = dict(
        line="3", confidence=0.9, explanation="Редкая линия", needs_review=True,
    )
    classifier = Mock()
    classifier.predict.return_value = CategoryPrediction(
        category="Тест", confidence=1, explanation="Тест", needs_manual_review=False,
    )
    service = AnalysisService(classifier=classifier, router=routing.RoutingAdapter(runtime))
    assert service.analyze(AppealInput(**APPEAL)).manual_review_required is True


def test_injected_analysis_does_not_load_bundled_model(tmp_path, monkeypatch):
    loader = Mock(side_effect=AssertionError("unexpected load"))
    monkeypatch.setattr(routing, "load_assistant", loader)
    service = AnalysisService()
    with TestClient(create_app(tmp_path / "runtime", analysis=service)) as client:
        assert client.app.state.analysis_service is service
        assert client.get("/api/integrations").json()["routing"] == "pending"
    loader.assert_not_called()
