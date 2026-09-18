"""Lazy, offline inference. Training is available only in train_categorizer.py."""
from __future__ import annotations

import json
import logging
from importlib.metadata import version
from pathlib import Path
from threading import Lock

from .features import (
    DEFAULT_THRESHOLD, EMBEDDER_NAME, EMBEDDING_DIM, FEATURE_VERSION,
    OTHER_CATEGORY, build_features, clean_text,
)

MODEL_PATH = Path(__file__).resolve().parent / "model.pkl"
RUNTIME_PACKAGES = ("scikit-learn", "joblib", "numpy", "sentence-transformers", "transformers", "torch")
logger = logging.getLogger(__name__)


def package_versions():
    return {name: version(name) for name in RUNTIME_PACKAGES}


def load_embedder(revision: str):
    # A pinned snapshot must already be in the local HF cache. Never download in HTTP.
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(
        EMBEDDER_NAME, revision=revision, local_files_only=True,
        trust_remote_code=False, device="cpu",
    )


class TicketCategorizer:
    def __init__(self, model_path=MODEL_PATH, threshold=DEFAULT_THRESHOLD):
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        self.model_path = Path(model_path)
        self.metadata_path = self.model_path.with_suffix(".json")
        self.threshold = threshold
        self.model = None
        self.embedder = None
        if not self.model_path.is_file():
            self.status = "model-missing"
        elif not self.metadata_path.is_file():
            self.status = "metadata-missing"
        else:
            self.status = "uninitialized"
        self._lock = Lock()

    clean_text = staticmethod(clean_text)

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
            from sklearn.exceptions import InconsistentVersionWarning

            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            # Only an operator-provided, trusted local artifact may be loaded.
            with warnings.catch_warnings():
                warnings.simplefilter("error", InconsistentVersionWarning)
                bundle = joblib.load(self.model_path)
            if not isinstance(bundle, dict) or bundle.get("format_version") != 1:
                raise ValueError("Legacy artifact has no feature/version manifest; retrain it")
            bundled_metadata = {key: value for key, value in bundle.items() if key != "model"}
            if metadata != bundled_metadata:
                raise ValueError("External model metadata does not match the artifact")
            if bundle["feature_version"] != FEATURE_VERSION or bundle["embedder"] != EMBEDDER_NAME:
                raise ValueError("Incompatible feature contract")
            if bundle["embedding_dim"] != EMBEDDING_DIM or bundle["versions"] != package_versions():
                raise ValueError("Incompatible dimensions or library versions")
            revision = bundle["embedder_revision"]
            import re
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("Embedder revision must be an immutable commit SHA")
            model = bundle["model"]
            if model.n_features_in_ != EMBEDDING_DIM or not callable(model.predict_proba):
                raise ValueError("Incompatible classifier")
            if len(model.classes_) < 2 or list(model.classes_) != bundle["classes"]:
                raise ValueError("Incompatible classes")
        except Exception:
            logger.exception("Categorization artifact cannot be loaded")
            self.status = "model-incompatible"
            return
        try:
            embedder = load_embedder(revision)
            if embedder.get_sentence_embedding_dimension() != EMBEDDING_DIM:
                raise ValueError("Incompatible embedder dimension")
        except Exception:
            logger.exception("Local categorization embedder unavailable")
            self.status = "embedder-unavailable"
            return
        self.model, self.embedder = model, embedder
        self.status = "ready"

    def _fallback(self):
        reasons = {
            "model-missing": "Модель категоризации отсутствует. Требуется отдельное обучение.",
            "metadata-missing": "Метаданные модели категоризации отсутствуют.",
            "model-incompatible": "Артефакт категоризации или версии библиотек несовместимы.",
            "embedder-unavailable": "Локальные веса SentenceTransformer недоступны или несовместимы.",
            "inference-error": "Ошибка категоризации. Требуется ручной разбор.",
        }
        return dict(category=None, confidence=0.0, is_reliable=False,
                    explanation=reasons.get(self.status, "Категоризация недоступна."))

    def predict(self, text, service=None, component=None):
        # Serialize first load and inference so concurrent failures cannot expose stale state.
        with self._lock:
            self._initialize()
            if self.status != "ready":
                return self._fallback()
            try:
                import numpy as np

                embedding = self.embedder.encode(
                    [build_features(text, service, component)],
                    convert_to_numpy=True, normalize_embeddings=False, show_progress_bar=False,
                )
                if embedding.shape != (1, EMBEDDING_DIM) or not np.isfinite(embedding).all():
                    raise ValueError("Invalid embedding")
                proba = np.asarray(self.model.predict_proba(embedding))
                if (proba.shape != (1, len(self.model.classes_)) or not np.isfinite(proba).all()
                        or (proba < 0).any() or (proba > 1).any()
                        or not np.isclose(proba.sum(), 1)):
                    raise ValueError("Invalid classifier probabilities")
                index = int(proba[0].argmax())
                confidence = float(proba[0, index])
                category = str(self.model.classes_[index])
                reliable = confidence >= self.threshold and category not in {OTHER_CATEGORY, "Прочее"}
                return dict(
                    category=category if reliable else None, confidence=confidence,
                    is_reliable=reliable,
                    explanation=f"Уверенность модели: {confidence:.2f}. Порог: {self.threshold}."
                    + (" Требуется ручной разбор." if not reliable else ""),
                )
            except Exception:
                logger.exception("Categorization inference failed")
                self.status = "inference-error"
                self.model = self.embedder = None
                return self._fallback()
