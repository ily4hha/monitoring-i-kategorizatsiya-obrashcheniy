"""Shared training/inference implementation; only ordinary sklearn dependencies."""
from __future__ import annotations

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.svm import LinearSVC

from .features import DEFAULT_THRESHOLD, MIN_KNOWN_WORDS, OTHER_CATEGORY, STOP_WORDS

SEED = 42


def fit_classifier(texts, labels):
    # Keep vectorization INSIDE CV: each calibration fold learns its own vocabulary/IDF.
    features = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=40_000,
                                sublinear_tf=True, stop_words=STOP_WORDS,
                                token_pattern=r"(?u)\b[^\W\d_]{2,}\b")),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2,
                                max_features=60_000, sublinear_tf=True)),
    ])
    pipeline = Pipeline([
        ("features", features),
        ("linear", LinearSVC(C=1.0, class_weight="balanced", dual="auto",
                             random_state=SEED, max_iter=10_000)),
    ])
    # Out-of-fold sigmoid calibration, then one final pipeline fitted on all train rows.
    return CalibratedClassifierCV(
        pipeline, method="sigmoid", cv=StratifiedKFold(3, shuffle=True, random_state=SEED),
        ensemble=False, n_jobs=1,
    ).fit(texts, labels)


def fitted_pipeline(model):
    return model.calibrated_classifiers_[0].estimator


def known_word_counts(model, texts):
    word = dict(fitted_pipeline(model).named_steps["features"].transformer_list)["word"]
    vocabulary = word.vocabulary_
    analyze = word.build_analyzer()
    return np.array([len({term for term in analyze(text)
                          if " " not in term and term in vocabulary}) for text in texts])


def review_reason(category, confidence, known_words, threshold=DEFAULT_THRESHOLD):
    if known_words < MIN_KNOWN_WORDS:
        return "insufficient-features"
    if category == OTHER_CATEGORY:
        return "outside-top-15"
    # The source's broad catch-all is a top-15 label, but is not an actionable decision.
    if category == "Прочее":
        return "ambiguous-category"
    if confidence < threshold:
        return "low-confidence"
    return None


def explain_terms(model, text, index, limit=5):
    """Positive word contributions to the chosen class's pre-calibration linear score."""
    pipeline = fitted_pipeline(model)
    word = dict(pipeline.named_steps["features"].transformer_list)["word"]
    vector = word.transform([text]).tocsr()
    linear = pipeline.named_steps["linear"]
    weights = linear.coef_[index] if len(linear.classes_) > 2 else linear.coef_[0] * (1 if index else -1)
    contributions = vector.data * weights[vector.indices]
    names = word.get_feature_names_out()
    order = np.argsort(-contributions, kind="stable")
    return [str(names[vector.indices[i]]) for i in order if contributions[i] > 0][:limit]
