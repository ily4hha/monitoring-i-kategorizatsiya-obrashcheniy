"""Routing model adapter and active-dataset similarity search."""
from __future__ import annotations

import importlib
import logging
import math
from pathlib import Path
import sys
import threading
from typing import Any
import warnings

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from app.models import AppealInput, RoutingPrediction, SimilarAppeal
from app.services.dataset_store import DatasetStore


MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "routing"
MODEL_PATH = MODULE_DIR / "reports" / "routing" / "assistant.joblib"
logger = logging.getLogger(__name__)

TEXT_COLUMNS = ("Описание 2", "Описание", "Текст обращения", "Текст", "Тема", "description", "subject")
SERVICE_COLUMNS = ("Услуга", "service")
COMPONENT_COLUMNS = ("Компонент услуги 1 уровня", "Компонент услуги", "component")
APPEAL_NUMBER_COLUMNS = ("Номер запроса", "Номер обращения", "ticket_id")
CATEGORY_COLUMNS = ("Вид запроса", "category_original", "category_grouped", "category")
LINE_COLUMNS = ("Кем решен (группа)", "resolved_line", "support_line")
RESOLUTION_COLUMNS = ("Результат работ", "resolution", "result")


def appeal_text(appeal: AppealInput) -> str:
    # Match training's free-text features; category and final outcome are not inputs.
    return f"{appeal.subject}\n{appeal.description}"


def similarity_text(appeal: AppealInput) -> str:
    """Use only fields known when a new appeal is registered."""
    return _join_text(appeal.subject, appeal.description, appeal.service, appeal.component)


def _text_value(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _first(record: dict[str, Any], columns: tuple[str, ...]) -> str | None:
    for column in columns:
        value = _text_value(record.get(column))
        if value is not None:
            return value
    return None


def _join_text(*values: Any) -> str:
    return "\n".join(value for item in values if (value := _text_value(item)) is not None)


def _record_text(record: dict[str, Any]) -> str:
    values = []
    for column in (*TEXT_COLUMNS, *SERVICE_COLUMNS, *COMPONENT_COLUMNS):
        value = _text_value(record.get(column))
        if value is not None and value not in values:
            values.append(value)
    return _join_text(*values)


def load_assistant():
    """Only this repository-owned path may be deserialized; no request path input."""
    if not MODEL_PATH.is_file():
        raise FileNotFoundError(MODEL_PATH)
    # The artifact records postcode.routing.Assistant as its pickle class name.
    module_dir = str(MODULE_DIR)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    module = importlib.import_module("postcode.routing")
    if Path(module.__file__).resolve() != MODULE_DIR / "postcode" / "routing.py":
        raise ImportError("Unexpected postcode.routing package")
    from sklearn.exceptions import InconsistentVersionWarning

    with warnings.catch_warnings():
        warnings.simplefilter("error", InconsistentVersionWarning)
        assistant = module.Assistant.load(MODEL_PATH)
    if not isinstance(assistant, module.Assistant):
        raise TypeError("Unexpected routing artifact type")
    return assistant


class RoutingRuntime:
    """One shared model per application; failed initialization is not retried."""

    def __init__(self):
        self.assistant = None
        self.status = "pending"
        self.reason = "Модель маршрутизации ещё не загружена; требуется ручной разбор."
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        try:
            self.assistant = load_assistant()
            # Exercise routing with an in-vocabulary query before marking ready.
            word = str(self.assistant.vectorizer.get_feature_names_out()[0])
            probe = AppealInput(subject=word[:500], description=word)
            _recommend(self.assistant, probe)
            self.status = "ready"
        except FileNotFoundError:
            self.fail("model-missing", "Файл модели маршрутизации отсутствует")
        except Exception:
            logger.exception("Bundled routing model failed initialization")
            self.fail("model-incompatible", "Модель маршрутизации несовместима или повреждена")

    def fail(self, code: str, reason: str):
        self.assistant = None
        self.status = f"unavailable:{code}"
        self.reason = f"{reason}; требуется ручной разбор."


def _recommend(assistant, appeal: AppealInput) -> RoutingPrediction:
    result = assistant.recommend_line(appeal_text(appeal))
    explanation = result["explanation"]
    if result.get("confidence_kind"):
        explanation += " " + result["confidence_kind"]
    return RoutingPrediction(
        support_line=result["line"], confidence=result["confidence"],
        explanation=explanation, needs_manual_review=result["needs_review"],
    )


class RoutingAdapter:
    def __init__(self, runtime: RoutingRuntime):
        self.runtime = runtime

    @property
    def status(self):
        return self.runtime.status

    def recommend(self, appeal: AppealInput, category: str | None) -> RoutingPrediction:
        if self.runtime.assistant is not None:
            try:
                return _recommend(self.runtime.assistant, appeal)
            except Exception:
                logger.exception("Routing inference failed")
                self.runtime.fail("inference-error", "Модель маршрутизации не смогла обработать обращение")
        return RoutingPrediction(
            support_line=None, confidence=0, explanation=self.runtime.reason,
            needs_manual_review=True,
        )


class SimilaritySearchAdapter:
    """In-memory TF-IDF index rebuilt from the active SQLite dataset."""

    def __init__(self, dataset_store: DatasetStore):
        self.dataset_store = dataset_store
        self._lock = threading.Lock()
        self._dataset_id: str | None = None
        self._records: tuple[dict[str, Any], ...] = ()
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None
        self._status = "pending"

    @property
    def status(self):
        return self._status

    def rebuild(self) -> None:
        """Build a complete replacement before publishing the new snapshot."""
        try:
            dataset = self.dataset_store.current_dataset()
            records = tuple(self.dataset_store.iter_active_records(dataset.dataset_id)) if dataset else ()
            searchable = tuple(record for record in records if _record_text(record))
            vectorizer = None
            matrix = None
            if searchable:
                vectorizer = TfidfVectorizer(
                    lowercase=True,
                    ngram_range=(1, 2),
                    sublinear_tf=True,
                    max_features=40_000,
                )
                try:
                    matrix = vectorizer.fit_transform([_record_text(record) for record in searchable])
                except ValueError as exc:
                    if "empty vocabulary" not in str(exc).lower():
                        raise
                    vectorizer = None
            with self._lock:
                self._dataset_id = dataset.dataset_id if dataset else None
                self._records = searchable
                self._vectorizer = vectorizer
                self._matrix = matrix
                self._status = "ready"
        except Exception:
            logger.exception("Active dataset similarity index failed to rebuild")
            with self._lock:
                self._dataset_id = None
                self._records = ()
                self._vectorizer = None
                self._matrix = None
                self._status = "unavailable:index-error"

    @staticmethod
    def _explanation(vectorizer, query_vector, record_vector) -> str | None:
        overlap = query_vector.multiply(record_vector)
        if not overlap.nnz:
            return None
        order = np.argsort(-overlap.data, kind="stable")[:5]
        features = vectorizer.get_feature_names_out()
        terms = [str(features[overlap.indices[index]]) for index in order]
        return "Совпали значимые термины: " + ", ".join(terms) + "."

    def search(self, appeal: AppealInput, limit: int = 5) -> list[SimilarAppeal]:
        limit = min(5, max(0, limit))
        # Service/component alone must not turn an empty appeal into a match.
        if limit == 0 or not appeal_text(appeal).strip():
            return []
        try:
            active = self.dataset_store.current_dataset()
            with self._lock:
                dataset_id = self._dataset_id
                records = self._records
                vectorizer = self._vectorizer
                matrix = self._matrix
            # Another process or worker may have activated a dataset. Rebuild
            # lazily as well as on this process's upload callback.
            if active is not None and active.dataset_id != dataset_id:
                self.rebuild()
                active = self.dataset_store.current_dataset()
                with self._lock:
                    dataset_id = self._dataset_id
                    records = self._records
                    vectorizer = self._vectorizer
                    matrix = self._matrix
            if active is None or active.dataset_id != dataset_id or vectorizer is None or matrix is None:
                return []

            query_vector = vectorizer.transform([similarity_text(appeal)])
            with self._lock:
                if self._vectorizer is vectorizer:
                    self._status = "ready"
            if not query_vector.nnz:
                return []
            scores = (matrix @ query_vector.T).toarray().ravel()
            hits = []
            for index in np.argsort(-scores, kind="stable"):
                score = float(scores[index])
                if score <= 0:
                    break
                if not math.isfinite(score) or score > 1 + 1e-10:
                    raise ValueError("Invalid similarity score")
                record = records[int(index)]
                record_id = _text_value(record.get("_record_id"))
                if record_id is None:
                    continue
                hits.append(SimilarAppeal(
                    record_id=record_id,
                    appeal_number=_first(record, APPEAL_NUMBER_COLUMNS),
                    score=min(1.0, max(0.0, score)),
                    category=_first(record, CATEGORY_COLUMNS),
                    support_line=_first(record, LINE_COLUMNS),
                    resolution=_first(record, RESOLUTION_COLUMNS),
                    explanation=self._explanation(vectorizer, query_vector, matrix[index]),
                ))
                if len(hits) == limit:
                    break
            return hits
        except Exception:
            logger.exception("Similarity inference failed")
            with self._lock:
                self._status = "unavailable:inference-error"
            return []
