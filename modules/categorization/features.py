"""The shared, versioned input contract for training and inference."""

import re

FEATURE_VERSION = "description-service-component-v1"
EMBEDDER_NAME = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
EMBEDDING_DIM = 768
DEFAULT_THRESHOLD = 0.25
OTHER_CATEGORY = "Прочее / Неопределено"


def clean_text(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    text = text.lower()
    text = re.sub(
        r"тема письма[:\s]*|текст письма[:\s]*|индекс опс[:\s]*|добрый день[,!\s]*|здравствуйте[,!\s]*",
        " ", text,
    )
    text = re.sub(r"http\S+|www\S+", " <URL> ", text)
    text = re.sub(r"\b\d+\b", " <NUM> ", text)
    return re.sub(r"\s+", " ", text).strip()


def metadata_text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "Не указано"


def build_features(text: str | None, service: str | None = None, component: str | None = None) -> str:
    return f"{clean_text(text)} | {metadata_text(service)} {metadata_text(component)}"
