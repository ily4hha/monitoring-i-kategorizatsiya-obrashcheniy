from __future__ import annotations

from io import BytesIO

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def make_workbook() -> bytes:
    buffer = BytesIO()
    frame = pd.DataFrame(
        [
            {"Тема": "Не приходит письмо", "Приоритет": "Высокий"},
            {"Тема": "Ошибка авторизации", "Приоритет": "Средний"},
        ]
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Обращения", index=False)
    return buffer.getvalue()


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_and_read_records() -> None:
    response = client.post(
        "/api/datasets",
        files={
            "file": (
                "appeals.xlsx",
                make_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 201
    dataset = response.json()
    assert dataset["row_count"] == 2
    assert dataset["active_sheet"] == "Обращения"

    records = client.get(f"/api/datasets/{dataset['dataset_id']}/records").json()
    assert records["total"] == 2
    assert records["items"][0]["Тема"] == "Не приходит письмо"


def test_pending_integrations_require_manual_review() -> None:
    response = client.post(
        "/api/appeals/analyze",
        json={"subject": "Проблема", "description": "Не удаётся выполнить действие"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["manual_review_required"] is True
    assert result["category"]["confidence"] == 0

