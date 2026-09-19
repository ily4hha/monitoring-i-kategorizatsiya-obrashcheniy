"""Thread-safe, offline inference from a bundled TF-IDF/linear artifact."""
from __future__ import annotations

import json
import logging
from importlib.metadata import version
from pathlib import Path
from threading import Lock

from .features import FEATURE_VERSION, MIN_KNOWN_WORDS, build_features, clean_text

MODEL_PATH = Path(__file__).resolve().parent / "model.pkl"
RUNTIME_PACKAGES = ("scikit-learn", "joblib", "numpy", "scipy")
logger = logging.getLogger(__name__)


def package_versions():
    return {name: version(name) for name in RUNTIME_PACKAGES}


class TicketCategorizer:
    def __init__(self, model_path=MODEL_PATH, threshold=None):
        if threshold is not None and not 0 <= threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        self.model_path = Path(model_path)
        self.metadata_path = self.model_path.with_suffix(".json")
        self.threshold = threshold
        self.model = None
        if not self.model_path.is_file():
            self.status = "model-missing"
        elif not self.metadata_path.is_file():
            self.status = "metadata-missing"
        else:
            self.status = "uninitialized"
        self._lock = Lock()

    clean_text = staticmethod(clean_text)

    def start(self):
        with self._lock:
            self._initialize()

    def _initialize(self):
        if self.status != "uninitialized":
            return
        if not self.model_path.is_file():
            self.status = "model-missing"
            return
        if not self.metadata_path.is_file():
            self.status = "metadata-missing"
            return
        try:
            import joblib
            import warnings
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.exceptions import InconsistentVersionWarning
            from .model import fitted_pipeline

            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            # Only operator-controlled local artifacts; never accept paths from HTTP/uploads.
            with warnings.catch_warnings():
                warnings.simplefilter("error", InconsistentVersionWarning)
                bundle = joblib.load(self.model_path)
            if not isinstance(bundle, dict) or bundle.get("format_version") != 2:
                raise ValueError("Unsupported categorization artifact")
            if metadata != {key: value for key, value in bundle.items() if key != "model"}:
                raise ValueError("External metadata does not match the artifact")
            if bundle["feature_version"] != FEATURE_VERSION:
                raise ValueError("Incompatible feature contract")
            # sklearn's serialized estimator contract is pinned in pyproject.toml.
            # Record (but do not require exact patch versions of) numpy/scipy/Python.
            if bundle["versions"]["scikit-learn"] != version("scikit-learn"):
                raise ValueError("Incompatible scikit-learn version")
            model = bundle["model"]
            if not isinstance(model, CalibratedClassifierCV) or model.ensemble is not False:
                raise ValueError("Incompatible classifier")
            if len(model.classes_) < 2 or list(model.classes_) != bundle["classes"]:
                raise ValueError("Incompatible classes")
            from .features import OTHER_CATEGORY
            if set(model.classes_) != set(bundle["top_categories"]) | ({OTHER_CATEGORY} if OTHER_CATEGORY in model.classes_ else set()):
                raise ValueError("Invalid top-category mapping")
            if (not 0 <= bundle["threshold"] <= 1 or bundle["min_known_words"] != MIN_KNOWN_WORDS
                    or not 1 <= len(bundle["top_categories"]) <= 15):
                raise ValueError("Invalid review policy")
            pipeline = fitted_pipeline(model)
            if set(dict(pipeline.named_steps["features"].transformer_list)) != {"word", "char"}:
                raise ValueError("Invalid feature pipeline")
            # A small offline probe catches incompatible scipy/numpy/artifacts at startup.
            self._validate_probabilities(model.predict_proba([build_features("проверка модели")]), model)
            self.model = model
            if self.threshold is None:
                self.threshold = bundle["threshold"]
            self.status = "ready"
        except Exception:
            logger.exception("Categorization artifact cannot be loaded")
            self.status = "model-incompatible"

    @staticmethod
    def _validate_probabilities(probabilities, model):
        import numpy as np
        proba = np.asarray(probabilities)
        if (proba.shape != (1, len(model.classes_)) or not np.isfinite(proba).all()
                or (proba < 0).any() or (proba > 1).any() or not np.isclose(proba.sum(), 1)):
            raise ValueError("Invalid classifier probabilities")
        return proba

    def _fallback(self):
        reasons = {
            "model-missing": "Поставляемый файл модели категоризации отсутствует.",
            "metadata-missing": "Метаданные модели категоризации отсутствуют.",
            "model-incompatible": "Артефакт категоризации повреждён или несовместим с runtime.",
            "inference-error": "Модель категоризации не смогла обработать обращение.",
        }
        reason = reasons.get(self.status, "Категоризация недоступна.")
        return dict(category=None, confidence=0.0, is_reliable=False, needs_manual_review=True,
                    explanation=reason + " Требуется ручной разбор.",
                    limitation=f"Категоризация недоступна: {self.status}. {reason}")

    def predict(self, text, service=None, component=None):
        with self._lock:
            self._initialize()
            if self.status != "ready":
                return self._fallback()
            try:
                from .model import explain_terms, known_word_counts, review_reason
                features = build_features(text, service, component)
                count = int(known_word_counts(self.model, [features])[0])
                if count < MIN_KNOWN_WORDS:
                    return dict(category=None, confidence=0.0, is_reliable=False, needs_manual_review=True,
                                explanation="Недостаточно известных модели слов в описании, услуге и компоненте. Требуется ручной разбор.",
                                limitation="insufficient-features: нужно хотя бы два разных содержательных слова из словаря обучения.")
                proba = self._validate_probabilities(self.model.predict_proba([features]), self.model)
                index = int(proba[0].argmax())
                confidence = float(proba[0, index])
                category = str(self.model.classes_[index])
                reason = review_reason(category, confidence, count, self.threshold)
                terms = explain_terms(self.model, features, index)
                explanation = (f"Кандидат: {category}. Оценка модели: {confidence:.2f}; "
                               f"порог автоматического ответа: {self.threshold:.2f}. "
                               "Использованы описание, услуга и компонент. ")
                if terms:
                    explanation += "Положительный вклад слов в линейную оценку: " + ", ".join(terms) + ". "
                explanation += "Оценка модели не гарантирует правильность категории."
                limitations = {
                    "outside-top-15": "Модель предполагает категорию вне top-15; требуется ручной разбор.",
                    "ambiguous-category": "Кандидат «Прочее» слишком общий; требуется ручной разбор.",
                    "low-confidence": "Оценка модели ниже порога; требуется ручной разбор.",
                }
                limitation = f"{reason}: {limitations[reason]}" if reason else None
                if reason:
                    explanation += " " + limitations[reason]
                return dict(category=category if reason is None else None, confidence=confidence,
                            is_reliable=reason is None, needs_manual_review=reason is not None,
                            explanation=explanation, limitation=limitation)
            except Exception:
                logger.exception("Categorization inference failed")
                self.status = "inference-error"
                self.model = None
                return self._fallback()
