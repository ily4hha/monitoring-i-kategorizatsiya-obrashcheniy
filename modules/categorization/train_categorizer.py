"""Offline training CLI. Run from the repository root with python -m ... ."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path

from .features import (
    DEFAULT_THRESHOLD, FEATURE_VERSION, MIN_KNOWN_WORDS,
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
    invalid_rows = source_rows - len(frame)
    # Conflicting labels for identical features are excluded; identical features never cross splits.
    conflicts = frame.groupby("text")["label"].transform("nunique").gt(1)
    conflict_rows = int(conflicts.sum())
    consistent = frame[~conflicts]
    duplicate_rows = int(consistent.duplicated("text").sum())
    frame = consistent.drop_duplicates("text").sort_values(["text", "label"]).reset_index(drop=True)
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
        "source_name": path.name,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_rows": source_rows, "prepared_rows": len(frame), "conflict_rows_removed": conflict_rows,
        "invalid_rows_removed": invalid_rows, "duplicate_rows_removed": duplicate_rows,
        "train_original_class_counts": train_counts.to_dict(),
        "split_method": "sorted normalized feature deduplication, stratified 80/20; rare labels (<5) share a stratum",
        "train_rows": len(train), "test_rows": len(test), "seed": SEED, "test_size": 0.2,
        "top_categories": top, "train_class_counts": train["target"].value_counts().to_dict(),
        "test_class_counts": test["target"].value_counts().to_dict(),
        "split_feature_hashes": {
            name: [hashlib.sha256(text.encode()).hexdigest() for text in part["text"]]
            for name, part in (("train", train), ("test", test))
        },
    }
    return train, test, report


def evaluate(model, test):
    import numpy as np
    from sklearn.metrics import accuracy_score, f1_score, log_loss, precision_recall_fscore_support
    from .model import known_word_counts, review_reason

    texts = test["text"].tolist()
    proba = model.predict_proba(texts)
    predictions = model.classes_[proba.argmax(axis=1)]
    counts = known_word_counts(model, texts)
    reasons = [review_reason(label, score, count) for label, score, count in
               zip(predictions, proba.max(axis=1), counts)]
    reliable = np.array([reason is None for reason in reasons])
    labels = model.classes_.tolist()
    actual = test["target"].to_numpy()
    outside = actual == OTHER_CATEGORY
    top_mask = ~outside
    precision, recall, per_class_f1, support = precision_recall_fscore_support(
        actual, predictions, labels=labels, zero_division=0,
    )
    return {
        "holdout_accuracy": float(accuracy_score(actual, predictions)),
        "holdout_macro_f1": float(f1_score(actual, predictions, labels=labels, average="macro", zero_division=0)),
        "holdout_top15_accuracy": float(accuracy_score(actual[top_mask], predictions[top_mask])),
        "holdout_top15_macro_f1": float(f1_score(actual[top_mask], predictions[top_mask],
            labels=[label for label in labels if label != OTHER_CATEGORY], average="macro", zero_division=0)),
        "holdout_log_loss": float(log_loss(actual, proba, labels=labels)),
        "coverage": float(reliable.mean()),
        "fallback_rate": float(1 - reliable.mean()),
        "automatic_count": int(reliable.sum()),
        "automatic_correct": int((actual[reliable] == predictions[reliable]).sum()),
        "automatic_accuracy": float(accuracy_score(actual[reliable], predictions[reliable])) if reliable.any() else None,
        "outside_top15_count": int(outside.sum()),
        "outside_top15_sent_to_manual": int((outside & ~reliable).sum()),
        "outside_top15_manual_recall": float((~reliable[outside]).mean()) if outside.any() else None,
        "manual_reasons": {reason: reasons.count(reason) for reason in sorted(set(reasons) - {None})},
        "per_class_holdout": {
            label: {"precision": float(precision[i]), "recall": float(recall[i]),
                    "f1": float(per_class_f1[i]), "support": int(support[i]),
                    "automatic_count": int(((actual == label) & reliable).sum())}
            for i, label in enumerate(labels)
        },
    }


LIMITATIONS = [
    "Небольшой случайный holdout, а не временная или внешняя проверка; близкие шаблоны могут остаться в разных частях.",
    "Удалены конфликты меток и дубликаты нормализованных признаков; качество не распространяется на исключённые неоднозначные строки.",
    "Top-15 определены исключительно по очищенному train. Все остальные исходные виды объединены в отдельный класс ручного разбора.",
    "Модель может ошибочно принять неизвестную категорию за top-15; обнаружение всех новых видов не гарантировано.",
    "Прочее входит в top-15 по частоте, но всегда требует ручного разбора как слишком общий ответ.",
    "Confidence — оценка модели после sigmoid-калибровки, а не гарантия правильности. Порог 0.70 фиксирован до holdout.",
    "Словесное объяснение показывает положительный вклад слов в линейный score; также используются символьные n-граммы и калибровка.",
    "Политика недостаточных признаков требует два разных известных содержательных слова; это эвристика, не универсальная проверка смысла.",
    "Используются только описание, услуга, компонент. Исходный вид — только целевая метка; линия, результат, SLA, сроки и статусы не используются.",
    "Артефакт обучен только на train, без дообучения на test. Загруженный через API Excel не меняет классификатор.",
]


def write_report(report, path):
    metrics = report["metrics"]
    rows = ["# Категоризация: воспроизводимый holdout", "",
            f"Источник: `{report['source_name']}`. "
            f"SHA-256: `{report['source_sha256']}`.", "",
            f"Исходных строк: {report['source_rows']}; невалидных: {report['invalid_rows_removed']}; "
            f"конфликтных: {report['conflict_rows_removed']}; повторных: {report['duplicate_rows_removed']}. "
            f"После очистки: {report['prepared_rows']}. Train: {report['train_rows']}, test: {report['test_rows']}; seed=42.", "",
            "Нормализованные признаки уникальны и не пересекаются между train/test. "
            "Стратификация 80/20 по исходным меткам (редкие <5 объединены только для стратификации). "
            "Top-15 выбираются после split по train. Хеши всех строк split сохранены в model.json.", "",
            "TF-IDF: слова (1–2), char_wb (3–5); LinearSVC C=1, balanced. "
            "Sigmoid-калибровка на out-of-fold оценках трёх train-фолдов; словарь и IDF обучаются внутри каждого фолда. "
            "Итоговый pipeline переобучен на всём train; test не использовался для выбора параметров и порога 0.70.", "",
            "| Вид | Train | Test |", "| --- | ---: | ---: |"]
    for label in report["top_categories"] + ([OTHER_CATEGORY] if OTHER_CATEGORY in report["classes"] else []):
        rows.append(f"| {label} | {report['train_class_counts'].get(label, 0)} | {report['test_class_counts'].get(label, 0)} |")
    rows += ["", "Метрики сырых кандидатов на всём holdout (top-15 + агрегированный прочий класс):", "",
             f"- Accuracy: {metrics['holdout_accuracy']:.6f}; macro-F1: {metrics['holdout_macro_f1']:.6f}.",
             f"- Только истинные top-15: accuracy {metrics['holdout_top15_accuracy']:.6f}; macro-F1 по 15 меткам {metrics['holdout_top15_macro_f1']:.6f}.",
             f"- Log loss: {metrics['holdout_log_loss']:.6f}.",
             f"- Автоматически: {metrics['automatic_count']}/{report['test_rows']} ({metrics['coverage']:.2%}); "
             f"правильно: {metrics['automatic_correct']}; accuracy на принятых: {metrics['automatic_accuracy']}.",
             f"- Ручной разбор: {metrics['fallback_rate']:.2%}.",
             f"- За пределами top-15: {metrics['outside_top15_sent_to_manual']}/{metrics['outside_top15_count']} отправлены на ручной разбор.", "",
             "Область автоматического ответа ограничена политикой отказа. Полная per-class precision/recall/F1 и число принятых примеров каждого истинного класса — в model.json. "
             "Эти метрики относятся только к категоризации; маршрутизация может дополнительно потребовать ручной разбор.", "",
             "## Ограничения", ""] + [f"- {item}" for item in report["limitations"]]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def train_model(source: Path, output: Path):
    import joblib
    from .model import fit_classifier
    from .service import package_versions

    if output.exists() or output.with_suffix(".json").exists():
        raise ValueError("Output already exists; choose a new output path")
    train, test, report = prepare_split(source)
    model = fit_classifier(train["text"].tolist(), train["target"].tolist())
    report["metrics"] = evaluate(model, test)
    # Train-majority baseline evaluated against test, with no test-selected baseline label.
    majority = train["target"].value_counts().index[0]
    report["metrics"]["majority_baseline_accuracy"] = float((test["target"] == majority).mean())
    report.update({
        "format_version": 2, "feature_version": FEATURE_VERSION,
        "versions": package_versions(), "python": platform.python_version(),
        "classes": model.classes_.tolist(), "threshold": DEFAULT_THRESHOLD,
        "min_known_words": MIN_KNOWN_WORDS,
        "training": {"word_ngrams": [1, 2], "char_wb_ngrams": [3, 5], "min_df": 2,
                     "max_word_features": 40_000, "max_char_features": 60_000,
                     "classifier": "LinearSVC", "C": 1.0, "class_weight": "balanced",
                     "calibration": "sigmoid", "cv": 3, "ensemble": False,
                     "threshold_selection": "fixed before holdout", "seed": SEED},
        "limitations": LIMITATIONS,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        joblib.dump({**report, "model": model}, temporary, compress=3)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report, output.with_suffix(".report.md"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train a lightweight offline TF-IDF/linear categorizer")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check-data", action="store_true", help="Validate split without training")
    args = parser.parse_args(argv)
    try:
        if args.check_data:
            _, _, report = prepare_split(args.input)
            report.pop("split_feature_hashes")
        else:
            report = train_model(args.input, args.output)
            report = {"output": str(args.output), "metrics": report["metrics"]}
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(2, f"Training unavailable: {exc}\nNo model was trained successfully.\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
