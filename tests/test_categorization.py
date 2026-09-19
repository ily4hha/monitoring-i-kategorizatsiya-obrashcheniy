from concurrent.futures import ThreadPoolExecutor
import builtins
import json
import shutil
import subprocess
import sys
from unittest.mock import Mock

from fastapi.testclient import TestClient
import joblib
import numpy as np
import pandas as pd
import pytest

from app.main import create_app
from app.models import AppealInput, RoutingPrediction
from app.services.categorization import ClassifierAdapter
from app.services.integrations import AnalysisService
from modules.categorization import service, train_categorizer as training
from modules.categorization.features import build_features, OTHER_CATEGORY

APPEAL = dict(subject="Ошибка", description="Не работает отслеживание почтового отправления", service="Портал", component="ЛК")


@pytest.fixture
def loaded(tmp_path):
    path = tmp_path / "model.pkl"
    shutil.copyfile(service.MODEL_PATH, path)
    shutil.copyfile(service.MODEL_PATH.with_suffix(".json"), path.with_suffix(".json"))
    categorizer = service.TicketCategorizer(path)
    categorizer.start()
    assert categorizer.status == "ready"
    return categorizer


def test_no_model_starts_and_returns_honest_fallback_without_heavy_ml(tmp_path, monkeypatch):
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"sentence_transformers", "torch", "transformers"}:
            pytest.fail("Categorization must never import heavy ML dependencies")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
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


def test_standard_runtime_is_ready_offline_without_heavy_dependencies():
    code = """
import builtins, socket, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert name.split('.')[0] not in {'sentence_transformers', 'torch', 'transformers', 'huggingface_hub'}
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def denied(*args, **kwargs):
    raise AssertionError('Network or subprocess forbidden during inference')
socket.socket.connect = denied
import subprocess
subprocess.Popen = denied
from app.services.categorization import ClassifierAdapter
from app.models import AppealInput
adapter = ClassifierAdapter()
assert adapter.status == 'ready'
result = adapter.predict(AppealInput(subject='Ошибка', description='Не работает отслеживание почтового отправления'))
assert 0 <= result.confidence <= 1 and result.explanation
assert adapter.status == 'ready'
assert not any(name in sys.modules for name in ('sentence_transformers', 'torch', 'transformers'))
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_lazy_load_once_concurrently(tmp_path, monkeypatch):
    categorizer = service.TicketCategorizer()
    load = Mock(wraps=joblib.load)
    monkeypatch.setattr(joblib, "load", load)
    assert categorizer.status == "uninitialized"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: categorizer.predict(APPEAL["description"]), range(8)))
    assert len({r["confidence"] for r in results}) == 1
    load.assert_called_once_with(categorizer.model_path)
    assert categorizer.status == "ready"


def fixed_prediction(categorizer, monkeypatch, label, confidence):
    classes = list(categorizer.model.classes_)
    proba = np.full((1, len(classes)), (1 - confidence) / (len(classes) - 1))
    proba[0, classes.index(label)] = confidence
    predictor = Mock(return_value=proba)
    monkeypatch.setattr(categorizer.model, "predict_proba", predictor)
    return predictor


@pytest.mark.parametrize("label,confidence,reason", [
    ("Личный кабинет", 0.69, "low-confidence"),
    (OTHER_CATEGORY, 0.95, "outside-top-15"),
    ("Прочее", 0.95, "ambiguous-category"),
])
def test_uncertain_other_or_ambiguous_is_manual(loaded, monkeypatch, label, confidence, reason):
    fixed_prediction(loaded, monkeypatch, label, confidence)
    result = ClassifierAdapter(loaded).predict(AppealInput(**APPEAL))
    assert result.category is None and result.needs_manual_review
    assert result.confidence == confidence
    assert reason in result.limitation
    assert loaded.status == "ready"


@pytest.mark.parametrize("description,service_name,component", [
    ("", None, None), ("Здравствуйте! 123 http://example.org", None, None),
    ("не и в на", "", ""), ("zyxwvu qqqzzzz", "unknownzz", "xxxxzz"),
])
def test_insufficient_features(loaded, description, service_name, component):
    result = loaded.predict(description, service_name, component)
    assert result["category"] is None and result["needs_manual_review"]
    assert result["confidence"] == 0
    assert "insufficient-features" in result["limitation"]
    assert loaded.status == "ready"


def test_threshold_boundary_and_http_contract(loaded, monkeypatch, tmp_path):
    fixed_prediction(loaded, monkeypatch, "Личный кабинет", 0.70)
    classifier = ClassifierAdapter(loaded)
    router = Mock(status="ready")
    router.recommend.return_value = RoutingPrediction(support_line="Первая", confidence=0.9, explanation="Mock")
    with TestClient(create_app(tmp_path / "runtime", analysis=AnalysisService(classifier=classifier, router=router))) as client:
        response = client.post("/api/appeals/analyze", json=APPEAL)
        assert response.status_code == 200
        result = response.json()
        assert result["category"]["category"] == "Личный кабинет"
        assert result["category"]["confidence"] == 0.7
        assert result["category"]["needs_manual_review"] is False
        assert result["category"]["limitation"] is None
        assert "Оценка модели" in result["category"]["explanation"]
        assert result["manual_review_required"] is False


def test_actual_bundled_model_accepts_some_holdout_examples(tmp_path):
    _, test, _ = training.prepare_split(training.DEFAULT_INPUT)
    categorizer = service.TicketCategorizer()
    accepted = []
    # Real unseen examples, no changes to threshold or probability calculation.
    for _, row in test.iterrows():
        result = categorizer.predict(row["Описание 2"], row["Услуга"], row["Компонент услуги 1 уровня"])
        if result["is_reliable"]:
            accepted.append((row, result))
            if len(accepted) == 3:
                break
    assert len(accepted) == 3
    row, expected = accepted[0]
    with TestClient(create_app(tmp_path / "runtime")) as client:
        assert client.get("/api/integrations").json()["classification"] == "ready"
        response = client.post("/api/appeals/analyze", json={
            "subject": "Новый запрос", "description": row["Описание 2"],
            "service": row["Услуга"], "component": row["Компонент услуги 1 уровня"],
        })
        assert response.status_code == 200
        result = response.json()["category"]
        assert result["category"] == expected["category"]
        assert result["confidence"] == pytest.approx(expected["confidence"])
        assert "Положительный вклад слов" in result["explanation"]
        assert not result["needs_manual_review"]


def test_only_registration_fields_reach_classifier():
    categorizer = Mock(status="ready")
    categorizer.predict.return_value = dict(category="test", confidence=0.8, is_reliable=True,
                                           explanation="test", limitation=None)
    adapter = ClassifierAdapter(categorizer)
    first = adapter.predict(AppealInput(**APPEAL, priority="Высокий"))
    second = adapter.predict(AppealInput(**{**APPEAL, "subject": "другая тема"}, priority="Низкий"))
    assert first == second
    for call in categorizer.predict.call_args_list:
        assert call.args == (APPEAL["description"], APPEAL["service"], APPEAL["component"])


@pytest.mark.parametrize("mode,status", [
    ("no-model", "model-missing"), ("no-metadata", "metadata-missing"),
    ("mismatch", "model-incompatible"), ("corrupt", "model-incompatible"),
])
def test_broken_artifact_is_honest_and_not_retried(loaded, monkeypatch, mode, status):
    categorizer = service.TicketCategorizer(loaded.model_path)
    if mode == "no-model":
        categorizer.model_path.unlink()
    elif mode == "no-metadata":
        categorizer.metadata_path.unlink()
    elif mode == "mismatch":
        categorizer.metadata_path.write_text("{}")
    else:
        categorizer.model_path.write_bytes(b"broken pickle")
    load = Mock(wraps=joblib.load)
    monkeypatch.setattr(joblib, "load", load)
    for _ in range(2):
        result = categorizer.predict("известные слова")
        assert result["category"] is None and result["confidence"] == 0
        assert status in result["limitation"]
    assert categorizer.status == status
    assert load.call_count <= 1


@pytest.mark.parametrize("field,value", [
    ("format_version", 1), ("feature_version", "wrong"), ("versions", {}),
    ("classes", []), ("top_categories", []), ("threshold", 2), ("min_known_words", 0),
])
def test_incompatible_consistent_bundle_fails_closed(loaded, field, value):
    bundle = joblib.load(loaded.model_path)
    bundle[field] = value
    joblib.dump(bundle, loaded.model_path)
    loaded.metadata_path.write_text(json.dumps({k: v for k, v in bundle.items() if k != "model"}))
    fresh = service.TicketCategorizer(loaded.model_path)
    fresh.start()
    assert fresh.status == "model-incompatible"
    assert fresh.predict("text")["needs_manual_review"]


@pytest.mark.parametrize("probabilities", [
    [[float("nan"), 0]], [[1.2, -0.2]], [[0.1, 0.1]], [[0.5]],
])
def test_invalid_predictions_fail_closed(loaded, monkeypatch, probabilities):
    predictor = Mock(return_value=probabilities)
    monkeypatch.setattr(loaded.model, "predict_proba", predictor)
    for _ in range(2):
        result = loaded.predict(APPEAL["description"])
        assert result["category"] is None and result["confidence"] == 0
    assert loaded.status == "inference-error"
    predictor.assert_called_once()


def test_inference_exception_does_not_break_http(loaded, monkeypatch, tmp_path):
    monkeypatch.setattr(loaded.model, "predict_proba", Mock(side_effect=RuntimeError("failure")))
    with TestClient(create_app(tmp_path / "runtime", analysis=AnalysisService(classifier=ClassifierAdapter(loaded)))) as client:
        response = client.post("/api/appeals/analyze", json=APPEAL)
        assert response.status_code == 200
        assert response.json()["manual_review_required"] is True
        assert "inference-error" in response.json()["category"]["limitation"]
        assert client.get("/api/integrations").json()["classification"] == "inference-error"


def write_training_data(path):
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
    assert all(text.endswith("| портал | ") for text in train["text"])


def test_real_dataset_split_and_top15_are_train_only():
    train, test, report = training.prepare_split(training.DEFAULT_INPUT)
    assert report["source_rows"] == 1931
    counts = train["label"].value_counts()
    expected_top = sorted(counts.index, key=lambda label: (-int(counts[label]), label))[:15]
    assert report["top_categories"] == expected_top
    assert len(expected_top) == 15
    assert set(test.loc[~test["label"].isin(expected_top), "target"]) == {OTHER_CATEGORY}
    assert set(report["split_feature_hashes"]["train"]).isdisjoint(report["split_feature_hashes"]["test"])
    assert len(train) + len(test) == report["prepared_rows"]
    assert report["source_rows"] == sum(report[key] for key in (
        "prepared_rows", "invalid_rows_removed", "conflict_rows_removed", "duplicate_rows_removed"))


def test_outcome_fields_never_affect_training_features(tmp_path):
    source = tmp_path / "train.csv"
    write_training_data(source)
    train, test, report = training.prepare_split(source)
    frame = pd.read_csv(source)
    for column in ("Кем решен (группа)", "Результат работ", "SLA", "Статус", "Фактическое время выполнения"):
        frame[column] = frame["Вид запроса"]  # would leak the answer if read
    frame.to_csv(source, index=False)
    second_train, second_test, second_report = training.prepare_split(source)
    assert train["text"].tolist() == second_train["text"].tolist()
    assert test["text"].tolist() == second_test["text"].tolist()
    assert report["split_feature_hashes"] == second_report["split_feature_hashes"]


def test_holdout_frequencies_cannot_promote_a_category_to_top15(tmp_path, monkeypatch):
    labels = [f"class{chr(97 + i)}" for i in range(16)]
    rows = [{"Описание 2": f"описание {label} {chr(97 + i // 26)}{chr(97 + i % 26)}",
             "Услуга": "портал", "Компонент услуги 1 уровня": "", "Вид запроса": label}
            for label in labels for i in range(40 if label == labels[-1] else 6)]
    source = tmp_path / "train.csv"
    pd.DataFrame(rows).to_csv(source, index=False)
    def controlled_split(frame, **kwargs):
        train = frame.groupby("label").head(5)
        return train, frame.drop(train.index)
    monkeypatch.setattr("sklearn.model_selection.train_test_split", controlled_split)
    train, test, report = training.prepare_split(source)
    assert report["top_categories"] == labels[:15]
    assert test["label"].value_counts().index[0] == labels[-1]
    assert set(train.loc[train["label"] == labels[-1], "target"]) == {OTHER_CATEGORY}
    assert set(test.loc[test["label"] == labels[-1], "target"]) == {OTHER_CATEGORY}


def test_training_roundtrip_reproducible_real_linear_model(tmp_path):
    source = tmp_path / "train.csv"
    write_training_data(source)
    outputs = [tmp_path / "one.pkl", tmp_path / "two.pkl"]
    reports = [training.train_model(source, output) for output in outputs]
    assert reports[0] == reports[1]
    assert reports[0]["metrics"]["holdout_accuracy"] == 1
    assert reports[0]["metrics"]["holdout_macro_f1"] == 1
    for output in outputs:
        categorizer = service.TicketCategorizer(output)
        result = categorizer.predict("описание alpha a", "портал")
        assert result["category"] == "alpha" and result["is_reliable"]
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="already exists"):
        training.train_model(source, outputs[0])


def test_bundled_holdout_report_matches_real_predictions():
    _, test, split = training.prepare_split(training.DEFAULT_INPUT)
    bundle = joblib.load(service.MODEL_PATH)
    assert split["split_feature_hashes"] == bundle["split_feature_hashes"]
    actual = training.evaluate(bundle["model"], test)
    for name in actual:
        assert actual[name] == bundle["metrics"][name]


def test_training_cli_needs_no_embedder(tmp_path):
    source = tmp_path / "train.csv"
    write_training_data(source)
    output = tmp_path / "model.pkl"
    command = [sys.executable, "-m", "modules.categorization.train_categorizer",
               "--input", str(source), "--output", str(output)]
    checked = subprocess.run(command + ["--check-data"], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["prepared_rows"] == 39
    trained = subprocess.run(command, capture_output=True, text=True)
    assert trained.returncode == 0, trained.stderr
    assert output.exists()
