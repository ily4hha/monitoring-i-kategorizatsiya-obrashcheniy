from concurrent.futures import ThreadPoolExecutor
import builtins
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import joblib
import numpy as np
import pandas as pd
import pytest

from app.main import create_app
from app.models import AppealInput, CategoryPrediction, RoutingPrediction
from app.services.categorization import ClassifierAdapter
from app.services.integrations import AnalysisService
from modules.categorization import service, train_categorizer as training
from modules.categorization.features import build_features, EMBEDDING_DIM, OTHER_CATEGORY

APPEAL = dict(subject="Ошибка", description="Добрый день! Не работает 123", service="Портал", component="ЛК")


def mock_bundle():
    model = Mock(n_features_in_=EMBEDDING_DIM)
    model.classes_ = np.array(["Личный кабинет", OTHER_CATEGORY])
    model.predict_proba.return_value = np.array([[0.8, 0.2]])
    return dict(format_version=1, feature_version=service.FEATURE_VERSION,
                embedder=service.EMBEDDER_NAME, embedder_revision="a" * 40,
                embedding_dim=EMBEDDING_DIM, versions={"test": "1"},
                model=model, classes=model.classes_.tolist())


@pytest.fixture
def loaded_mock(tmp_path, monkeypatch):
    path = tmp_path / "model.pkl"
    path.touch()
    bundle = mock_bundle()
    path.with_suffix(".json").write_text(json.dumps({key: value for key, value in bundle.items() if key != "model"}))
    load = Mock(return_value=bundle)
    monkeypatch.setattr(joblib, "load", load)
    monkeypatch.setattr(service, "package_versions", lambda: {"test": "1"})
    embedder = Mock()
    embedder.get_sentence_embedding_dimension.return_value = EMBEDDING_DIM
    embedder.encode.return_value = np.zeros((1, EMBEDDING_DIM))
    factory = Mock(return_value=embedder)
    monkeypatch.setattr(service, "load_embedder", factory)
    return service.TicketCategorizer(path), bundle, load, factory, embedder


def test_no_model_starts_and_returns_honest_fallback_without_ml(tmp_path, monkeypatch):
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"sentence_transformers", "torch", "transformers"}:
            pytest.fail("Missing model must never import heavy ML dependencies")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(service, "load_embedder", Mock(side_effect=AssertionError("No embedder expected")))
    adapter = ClassifierAdapter(service.TicketCategorizer(tmp_path / "absent.pkl"))
    with TestClient(create_app(tmp_path / "runtime", analysis=AnalysisService(classifier=adapter))) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/integrations").json()["classification"] == "model-missing"
        for _ in range(2):
            response = client.post("/api/appeals/analyze", json=APPEAL)
            assert response.status_code == 200
            result = response.json()
            assert result["category"]["category"] is None
            assert result["category"]["confidence"] == 0
            assert result["category"]["needs_manual_review"] is True
            assert "model-missing" in result["category"]["limitation"]
            assert result["manual_review_required"] is True
    service.load_embedder.assert_not_called()


def test_import_has_no_optional_ml_dependency():
    code = """
import sys
from app.main import create_app
from modules.categorization.service import TicketCategorizer
assert not any(name in sys.modules for name in ('sentence_transformers', 'torch', 'transformers'))
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_lazy_load_once_concurrently_and_shared_features(loaded_mock):
    categorizer, bundle, load, factory, embedder = loaded_mock
    adapter = ClassifierAdapter(categorizer)
    assert adapter.status == "uninitialized"
    load.assert_not_called()
    factory.assert_not_called()
    appeal = AppealInput(**APPEAL)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(adapter.predict, [appeal] * 8))
    assert all(r.category == "Личный кабинет" and not r.needs_manual_review for r in results)
    load.assert_called_once_with(categorizer.model_path)
    factory.assert_called_once_with("a" * 40)
    assert adapter.status == "ready"
    bundle["model"].fit.assert_not_called()
    assert embedder.encode.call_args.args[0] == [build_features(APPEAL["description"], "Портал", "ЛК")]
    assert build_features("Здравствуйте! Ошибка 123 http://example.org") == "ошибка <NUM> <URL> | Не указано Не указано"


@pytest.mark.parametrize("proba,threshold", [([0.6, 0.4], 0.7), ([0.1, 0.9], 0.25)])
def test_uncertain_or_other_prediction_is_manual(loaded_mock, proba, threshold):
    categorizer, bundle, *_ = loaded_mock
    categorizer.threshold = threshold
    bundle["model"].predict_proba.return_value = np.array([proba])
    result = ClassifierAdapter(categorizer).predict(AppealInput(**APPEAL))
    assert result.category is None and result.needs_manual_review
    assert result.confidence == max(proba)


def test_plain_other_category_is_manual(loaded_mock):
    categorizer, bundle, *_ = loaded_mock
    bundle["model"].classes_ = np.array(["Личный кабинет", "Прочее"])
    bundle["classes"] = bundle["model"].classes_.tolist()
    categorizer.metadata_path.write_text(json.dumps({key: value for key, value in bundle.items() if key != "model"}))
    bundle["model"].predict_proba.return_value = np.array([[0.1, 0.9]])

    result = ClassifierAdapter(categorizer).predict(AppealInput(**APPEAL))

    assert result.category is None
    assert result.confidence == 0.9
    assert result.needs_manual_review is True


def test_missing_or_mismatched_external_metadata_never_becomes_ready(loaded_mock):
    categorizer, _, load, factory, _ = loaded_mock
    categorizer.metadata_path.unlink()
    categorizer = service.TicketCategorizer(categorizer.model_path)
    assert categorizer.status == "metadata-missing"
    adapter = ClassifierAdapter(categorizer)
    with TestClient(create_app(categorizer.model_path.parent / "runtime", analysis=AnalysisService(classifier=adapter))) as client:
        assert client.get("/api/integrations").json()["classification"] == "metadata-missing"
    assert categorizer.predict("text")["category"] is None
    load.assert_not_called()
    factory.assert_not_called()

    categorizer.metadata_path.write_text("{}")
    categorizer = service.TicketCategorizer(categorizer.model_path)
    assert categorizer.predict("text")["category"] is None
    assert categorizer.status == "model-incompatible"
    factory.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("format_version", 0), ("feature_version", "wrong"), ("embedding_dim", 384),
    ("versions", {}), ("embedder", "other"), ("embedder_revision", "main"), ("classes", []),
])
def test_incompatible_bundle_does_not_load_embedder(loaded_mock, field, value):
    categorizer, bundle, load, factory, _ = loaded_mock
    bundle[field] = value
    for _ in range(2):
        assert categorizer.predict("text")["category"] is None
    assert categorizer.status == "model-incompatible"
    load.assert_called_once()
    factory.assert_not_called()


def test_corrupt_artifact_and_missing_file_after_constructor(loaded_mock):
    categorizer, _, load, factory, _ = loaded_mock
    load.side_effect = ValueError("corrupt pickle")
    assert categorizer.predict("text")["confidence"] == 0
    assert categorizer.status == "model-incompatible"
    factory.assert_not_called()
    fresh = service.TicketCategorizer(categorizer.model_path)
    fresh.model_path.unlink()
    assert fresh.predict("text")["confidence"] == 0
    assert fresh.status == "model-missing"
    load.assert_called_once()


def test_missing_local_embedder_falls_back_without_retry(loaded_mock):
    categorizer, _, load, factory, _ = loaded_mock
    factory.side_effect = OSError("snapshot not cached")
    for _ in range(2):
        assert categorizer.predict("text")["confidence"] == 0
    assert categorizer.status == "embedder-unavailable"
    factory.assert_called_once()
    load.assert_called_once()


def test_embedder_loading_is_strictly_offline(monkeypatch):
    constructor = Mock()
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    service.load_embedder("a" * 40)
    constructor.assert_called_once_with(service.EMBEDDER_NAME, revision="a" * 40,
                                        local_files_only=True, trust_remote_code=False, device="cpu")


@pytest.mark.parametrize("probabilities", [
    [[float("nan"), 0]], [[1.2, -0.2]], [[0.1, 0.1]], [[0.5]],
])
def test_invalid_predictions_fail_closed(loaded_mock, probabilities):
    categorizer, bundle, _, _, _ = loaded_mock
    bundle["model"].predict_proba.return_value = probabilities
    result = categorizer.predict("text")
    assert result["category"] is None and result["confidence"] == 0
    assert categorizer.status == "inference-error"
    assert categorizer.predict("text")["confidence"] == 0
    bundle["model"].predict_proba.assert_called_once()


def test_inference_exception_does_not_break_http(loaded_mock, tmp_path):
    categorizer, _, _, _, embedder = loaded_mock
    embedder.encode.side_effect = RuntimeError("failure")
    with TestClient(create_app(tmp_path / "runtime", analysis=AnalysisService(classifier=ClassifierAdapter(categorizer)))) as client:
        response = client.post("/api/appeals/analyze", json=APPEAL)
        assert response.status_code == 200
        assert response.json()["manual_review_required"] is True
        assert client.get("/api/integrations").json()["classification"] == "inference-error"


def test_http_with_mock_classifier_preserves_contract(tmp_path):
    classifier = Mock(status="ready")
    classifier.predict.return_value = CategoryPrediction(
        category="Личный кабинет", confidence=0.9, explanation="Mock", needs_manual_review=False)
    router = Mock(status="ready")
    router.recommend.return_value = RoutingPrediction(support_line="Первая", confidence=0.9, explanation="Mock")
    with TestClient(create_app(tmp_path / "runtime", analysis=AnalysisService(classifier=classifier, router=router))) as client:
        result = client.post("/api/appeals/analyze", json=APPEAL).json()
        assert result["category"]["category"] == "Личный кабинет"
        assert result["manual_review_required"] is False
        classifier.predict.assert_called_once_with(AppealInput(**APPEAL))


def write_training_data(path):
    # Alphabetic suffixes remain distinct after number normalization.
    rows = [{"Описание 2": f"описание {label} {chr(97 + i)}", "Услуга": "портал",
             "Компонент услуги 1 уровня": "", "Вид запроса": label}
            for label in ("alpha", "beta") for i in range(20)]
    rows += [dict(rows[0]), {**rows[1], "Вид запроса": "conflict"}]
    pd.DataFrame(rows).to_csv(path, index=False)


def test_split_reproducible_and_no_duplicate_feature_leakage(tmp_path):
    source = tmp_path / "train.csv"
    write_training_data(source)
    train, test, report = training.prepare_split(source)
    _, _, again = training.prepare_split(source)
    assert report == again
    assert report["conflict_rows_removed"] == 2
    assert report["prepared_rows"] == 39
    assert set(train["text"]).isdisjoint(test["text"])
    assert all(text.endswith("| портал Не указано") for text in train["text"])


def test_real_dataset_preflight_without_embedder():
    train, test, report = training.prepare_split(training.DEFAULT_INPUT)
    assert report["source_rows"] == 1931
    assert len(train) == 1440 and len(test) == 361
    assert min(report["train_class_counts"].values()) >= 3
    assert set(report["split_feature_hashes"]["train"]).isdisjoint(report["split_feature_hashes"]["test"])


def test_training_roundtrip_with_mock_embedder_and_real_classifier(tmp_path, monkeypatch):
    source = tmp_path / "train.csv"
    write_training_data(source)
    monkeypatch.setattr(service, "package_versions", lambda: {"test": "1"})
    monkeypatch.setitem(sys.modules, "torch", Mock())
    embedder = Mock()
    def encode(texts, **kwargs):
        return np.array([[float("alpha" in text)] * EMBEDDING_DIM for text in texts])
    embedder.encode.side_effect = encode
    embedder.get_sentence_embedding_dimension.return_value = EMBEDDING_DIM
    monkeypatch.setattr(service, "load_embedder", Mock(return_value=embedder))
    outputs = [tmp_path / "one.pkl", tmp_path / "two.pkl"]
    reports = [training.train_model(source, output, "a" * 40) for output in outputs]
    assert reports[0] == reports[1]
    assert reports[0]["metrics"]["holdout_accuracy"] == 1
    assert reports[0]["metrics"]["holdout_macro_f1"] == 1
    assert reports[0]["metrics"]["coverage"] == 1
    assert reports[0]["metrics"]["fallback_rate"] == 0
    assert reports[0]["metrics"]["automatic_macro_f1"] == 1
    assert set(reports[0]["metrics"]["per_class_holdout"]) == {"alpha", "beta"}
    for output in outputs:
        manifest = json.loads(output.with_suffix(".json").read_text())
        assert manifest["feature_version"] == service.FEATURE_VERSION
        categorizer = service.TicketCategorizer(output)
        result = categorizer.predict("описание alpha a", "портал")
        assert result["category"] == "alpha" and result["is_reliable"]
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="already exists"):
        training.train_model(source, outputs[0], "a" * 40)


def test_training_preflight_cli_and_failure_create_no_artifact(tmp_path):
    source = tmp_path / "train.csv"
    write_training_data(source)
    output = tmp_path / "model.pkl"
    command = [sys.executable, "-m", "modules.categorization.train_categorizer",
               "--input", str(source), "--output", str(output)]
    checked = subprocess.run(command + ["--check-data"], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["prepared_rows"] == 39
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode == 2
    assert not output.exists()
