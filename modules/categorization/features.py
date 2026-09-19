"""The shared, versioned input contract for training and inference."""

import re

FEATURE_VERSION = "description-service-component-tfidf-v2"
# Fixed before holdout evaluation, not selected using test labels.
DEFAULT_THRESHOLD = 0.70
OTHER_CATEGORY = "Прочее / Неопределено"
MIN_KNOWN_WORDS = 2
STOP_WORDS = "и в во на с со по за из для от до к ко у о об а но или не нет это как что при все вы мы я он она они num url неуказано".split()


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
    return clean_text(value)


def build_features(text: str | None, service: str | None = None, component: str | None = None) -> str:
    return f"{clean_text(text)} | {metadata_text(service)} | {metadata_text(component)}"
