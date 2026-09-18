"""Offline training CLI. Run from the repository root with python -m ... ."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from importlib.metadata import distributions
from pathlib import Path

from .features import (
    DEFAULT_THRESHOLD, EMBEDDER_NAME, EMBEDDING_DIM, FEATURE_VERSION,
    OTHER_CATEGORY, build_features,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "modules/routing/data/raw/Обращения_1931.xlsx"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "model.pkl"
SEED = 42


def prepare_split(path: Path):
    """Validate data and split before selecting business classes; no ML imports/downloads."""
    import pandas as pd
    from sklearn.model_selection import train_test_split

    frame = pd.read_excel(path) if path.suffix.lower() == ".xlsx" else pd.read_csv(path)
    required = ["Описание 2", "Услуга", "Компонент услуги 1 уровня", "Вид запроса"]
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"Missing training columns: {missing}")
    source_rows = len(frame)
    frame = frame[required].fillna("").copy()
    frame = frame[frame["Описание 2"].astype(str).str.strip().ne("")
                  & frame["Вид запроса"].astype(str).str.strip().ne("")].copy()
    frame["label"] = frame["Вид запроса"].astype(str).str.strip()
    if frame["label"].isin([OTHER_CATEGORY, "ОПЕРАТОР"]).any():
        raise ValueError("Training requires original labels, not fallback labels")
    frame["text"] = [build_features(*row) for row in frame[required[:3]].itertuples(index=False, name=None)]
    # Conflicting labels for identical features are excluded; identical features never cross splits.
    conflicts = frame.groupby("text")["label"].transform("nunique").gt(1)
    conflict_rows = int(conflicts.sum())
    frame = frame[~conflicts].drop_duplicates("text").sort_values(["text", "label"]).reset_index(drop=True)
    counts = frame["label"].value_counts()
    if len(counts) < 2:
        raise ValueError("At least two categories are required")
    # Rare source labels share a stratum, but retain their true labels until the train-only mapping.
    strata = frame["label"].where(frame["label"].map(counts).ge(5), OTHER_CATEGORY)
    if strata.value_counts().min() < 2:
        raise ValueError("Too few rare examples for a stratified holdout")
    train, test = train_test_split(frame, test_size=0.2, random_state=SEED, stratify=strata)
    train_counts = train["label"].value_counts()
    top = sorted(train_counts.index, key=lambda label: (-int(train_counts[label]), label))[:15]
    train, test = train.copy(), test.copy()
    for part in (train, test):
        part["target"] = part["label"].where(part["label"].isin(top), OTHER_CATEGORY)
    if train["target"].nunique() < 2 or train["target"].value_counts().min() < 3:
        raise ValueError("Each training class needs at least three examples for calibration")
    report = {
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_rows": source_rows, "prepared_rows": len(frame), "conflict_rows_removed": conflict_rows,
        "train_rows": len(train), "test_rows": len(test), "seed": SEED, "test_size": 0.2,
        "top_categories": top, "train_class_counts": train["target"].value_counts().to_dict(),
        "test_class_counts": test["target"].value_counts().to_dict(),
        "split_feature_hashes": {
            name: [hashlib.sha256(text.encode()).hexdigest() for text in part["text"]]
            for name, part in (("train", train), ("test", test))
        },
    }
    return train, test, report


def fit_classifier(embeddings, labels):
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import RandomForestClassifier

    base = RandomForestClassifier(
        n_estimators=400, max_depth=20, min_samples_leaf=3,
        class_weight="balanced", random_state=SEED, n_jobs=1,
    )
    model = CalibratedClassifierCV(base, method="isotonic", cv=3, n_jobs=1)
    return model.fit(embeddings, labels)


def train_model(source: Path, output: Path, revision: str):
    import joblib
    import numpy as np
    from sklearn.metrics import accuracy_score, f1_score
    from .service import load_embedder, package_versions

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("--embedder-revision must be a 40-character commit SHA")
    if output.exists() or output.with_suffix(".json").exists():
        raise ValueError("Output already exists; choose a new output path")
    train, test, report = prepare_split(source)
    versions = package_versions()  # Fail before training if optional packages are missing.
    import torch

    torch.manual_seed(SEED)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    embedder = load_embedder(revision)
    texts = train["text"].tolist() + test["text"].tolist()
    embeddings = np.asarray(embedder.encode(
        texts, batch_size=32, show_progress_bar=True,
        convert_to_numpy=True, normalize_embeddings=False,
    ))
    if embeddings.shape != (len(texts), EMBEDDING_DIM) or not np.isfinite(embeddings).all():
        raise ValueError("Invalid embedder output")
    model = fit_classifier(embeddings[:len(train)], train["target"])
    proba = model.predict_proba(embeddings[len(train):])
    predictions = model.classes_[proba.argmax(axis=1)]
    reliable = (proba.max(axis=1) >= DEFAULT_THRESHOLD) & (predictions != OTHER_CATEGORY)
    report["metrics"] = {
        "holdout_accuracy": float(accuracy_score(test["target"], predictions)),
        "holdout_macro_f1": float(f1_score(test["target"], predictions, average="macro", zero_division=0)),
        "fallback_rate": float(1 - reliable.mean()),
        "automatic_count": int(reliable.sum()),
        "automatic_accuracy": (float(accuracy_score(test["target"][reliable], predictions[reliable]))
                               if reliable.any() else None),
    }
    report.update({
        "format_version": 1, "feature_version": FEATURE_VERSION,
        "embedder": EMBEDDER_NAME, "embedder_revision": revision, "embedding_dim": EMBEDDING_DIM,
        "versions": versions, "python": platform.python_version(),
        "environment": sorted(f"{d.metadata['Name']}=={d.version}" for d in distributions()),
        "classes": model.classes_.tolist(), "threshold": DEFAULT_THRESHOLD,
        "training": {"n_estimators": 400, "max_depth": 20, "min_samples_leaf": 3,
                     "class_weight": "balanced", "calibration": "isotonic", "cv": 3, "device": "cpu"},
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    # Publish the complete artifact only after successful fit/evaluation/serialization.
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        joblib.dump({**report, "model": model}, temporary, compress=3)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train categorization offline using a pre-cached SentenceTransformer")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--embedder-revision", help="Immutable commit SHA already present in the local HF cache")
    parser.add_argument("--check-data", action="store_true", help="Validate split without loading transformer or training")
    args = parser.parse_args(argv)
    try:
        if args.check_data:
            _, _, report = prepare_split(args.input)
            report.pop("split_feature_hashes")
        else:
            if not args.embedder_revision:
                parser.error("training requires --embedder-revision; weights must be cached locally")
            report = train_model(args.input, args.output, args.embedder_revision)
            report = {"output": str(args.output), "metrics": report["metrics"]}
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(2, f"Training unavailable: {exc}\nNo model was trained successfully.\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
