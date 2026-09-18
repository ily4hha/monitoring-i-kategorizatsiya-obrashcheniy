"""Adapters for the trusted, bundled routing model (never uploaded datasets)."""
from __future__ import annotations

import importlib
import logging
import math
from pathlib import Path
import sys
import warnings

from app.models import AppealInput, RoutingPrediction, SimilarAppeal


MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "routing"
MODEL_PATH = MODULE_DIR / "reports" / "routing" / "assistant.joblib"
logger = logging.getLogger(__name__)


def appeal_text(appeal: AppealInput) -> str:
    # Match training's free-text features; category and final outcome are not inputs.
    return f"{appeal.subject}\n{appeal.description}"


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
            # Exercise both paths with an in-vocabulary query before marking ready.
            word = str(self.assistant.vectorizer.get_feature_names_out()[0])
            probe = AppealInput(subject=word[:500], description=word)
            _recommend(self.assistant, probe)
            _search(self.assistant, probe, 1)
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


def _search(assistant, appeal: AppealInput, limit: int) -> list[SimilarAppeal]:
    results = assistant.find_similar(appeal_text(appeal), limit=limit)
    hits = []
    for item in results[:limit]:
        score = float(item["similarity"])
        if not math.isfinite(score) or not -1e-10 <= score <= 1 + 1e-10:
            raise ValueError("Invalid similarity score")
        hits.append(SimilarAppeal(
            record_id=str(item["id"]), score=min(1., max(0., score)),
            category=item["category"], support_line=item["line"], resolution=item["result"],
        ))
    return hits


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
    def __init__(self, runtime: RoutingRuntime):
        self.runtime = runtime

    @property
    def status(self):
        return self.runtime.status

    def search(self, appeal: AppealInput, limit: int = 5) -> list[SimilarAppeal]:
        limit = min(5, max(0, limit))
        if limit == 0 or self.runtime.assistant is None:
            return []
        try:
            return _search(self.runtime.assistant, appeal, limit)
        except Exception:
            logger.exception("Similarity inference failed")
            self.runtime.fail("inference-error", "Поиск похожих обращений недоступен")
            return []
