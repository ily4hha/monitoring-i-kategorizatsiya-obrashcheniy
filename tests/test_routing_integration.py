from unittest.mock import Mock

import joblib
import pytest
from fastapi.testclient import TestClient
from sklearn.exceptions import InconsistentVersionWarning

from app.main import create_app
from app.models import AppealInput, CategoryPrediction, RoutingPrediction, SimilarAppeal
from app.services import routing
from app.services.integrations import AnalysisService


APPEAL = {"subject": "Ошибка авторизации", "description": "Не могу войти в личный кабинет, пароль не работает"}


def test_real_model_loaded_once_at_startup_and_shared(tmp_path, monkeypatch):
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
        assert model is service.similarity.runtime.assistant
        statuses = client.get("/api/integrations").json()
        assert statuses == dict(classification="pending", routing="ready", similarity="ready", analytics="ready")
        expected = model.recommend_line(routing.appeal_text(AppealInput(**APPEAL)))
        expected_hits = model.find_similar(routing.appeal_text(AppealInput(**APPEAL)), limit=5)
        assert expected["line"] is not None and expected_hits
        for _ in range(3):
            response = client.post("/api/appeals/analyze", json=APPEAL)
            assert response.status_code == 200
            result = response.json()
            assert result["routing"]["support_line"] == expected["line"]
            assert result["routing"]["confidence"] == pytest.approx(expected["confidence"])
            assert result["routing"]["needs_manual_review"] == expected["needs_review"]
            assert expected["explanation"] in result["routing"]["explanation"]
            assert result["similar_appeals"] == [dict(
                record_id=str(hit["id"]), score=pytest.approx(hit["similarity"]),
                category=hit["category"], support_line=hit["line"], resolution=hit["result"],
            ) for hit in expected_hits]
        assert isinstance(service.router.recommend(AppealInput(**APPEAL), None), RoutingPrediction)
        for limit in [1, 5, 100]:
            hits = service.similarity.search(AppealInput(**APPEAL), limit)
            assert 0 < len(hits) <= min(limit, 5)
            assert all(isinstance(hit, SimilarAppeal) for hit in hits)
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
        assert status["routing"] == status["similarity"] == f"unavailable:{reason}"
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


@pytest.mark.parametrize("method", ["recommend_line", "find_similar"])
def test_inference_failure_degrades_without_reloading(tmp_path, monkeypatch, method):
    with TestClient(create_app(tmp_path / "runtime")) as client:
        runtime = client.app.state.analysis_service.router.runtime
        assert runtime.status == "ready"
        monkeypatch.setattr(runtime.assistant, method, Mock(side_effect=ValueError("broken inference")))
        response = client.post("/api/appeals/analyze", json=APPEAL)
        assert response.status_code == 200
        assert response.json()["similar_appeals"] == []
        assert client.get("/api/integrations").json()["routing"] == "unavailable:inference-error"
        monkeypatch.setattr(joblib, "load", Mock(side_effect=AssertionError("unexpected reload")))
        result = client.post("/api/appeals/analyze", json=APPEAL).json()
        assert result["routing"]["support_line"] is None
        assert result["routing"]["confidence"] == 0
        assert result["manual_review_required"] is True


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
