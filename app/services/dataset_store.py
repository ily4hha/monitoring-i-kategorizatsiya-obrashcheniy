from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import pandas as pd

from app.models import DatasetSheet, DatasetSummary, RecordPage


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".xls"}


class DatasetError(ValueError):
    pass


def _json_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^\w.() -]+", "_", Path(filename).name, flags=re.UNICODE)
    return cleaned[:180] or "dataset.xlsx"


class DatasetStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.upload_dir = runtime_dir / "uploads"
        self.db_path = runtime_dir / "app.db"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    active_sheet TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    columns_json TEXT NOT NULL,
                    sheets_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    record_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    sheet_name TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(dataset_id) REFERENCES datasets(id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_dataset
                    ON records(dataset_id, sheet_name, row_number);
                """
            )

    def import_excel(self, file: BinaryIO, filename: str) -> DatasetSummary:
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise DatasetError("Поддерживаются только файлы .xlsx и .xls")

        content = file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise DatasetError("Файл превышает ограничение 25 МБ")
        if not content:
            raise DatasetError("Загружен пустой файл")

        dataset_id = uuid.uuid4().hex
        stored_path = self.upload_dir / f"{dataset_id}-{_safe_filename(filename)}"
        stored_path.write_bytes(content)

        try:
            workbook = pd.ExcelFile(stored_path)
            frames = {
                sheet: pd.read_excel(workbook, sheet_name=sheet)
                for sheet in workbook.sheet_names
            }
        except Exception as exc:
            stored_path.unlink(missing_ok=True)
            raise DatasetError(f"Не удалось прочитать Excel: {exc}") from exc

        non_empty = [(name, frame) for name, frame in frames.items() if not frame.empty]
        if not non_empty:
            stored_path.unlink(missing_ok=True)
            raise DatasetError("В книге нет непустых листов")

        active_sheet, active_frame = max(non_empty, key=lambda item: len(item[1]))
        sheets = [
            DatasetSheet(
                name=name,
                row_count=len(frame),
                columns=[str(column).strip() for column in frame.columns],
            )
            for name, frame in frames.items()
        ]
        summary = DatasetSummary(
            dataset_id=dataset_id,
            filename=filename,
            active_sheet=active_sheet,
            row_count=len(active_frame),
            columns=[str(column).strip() for column in active_frame.columns],
            sheets=sheets,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO datasets
                    (id, filename, active_sheet, row_count, columns_json, sheets_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.dataset_id,
                    summary.filename,
                    summary.active_sheet,
                    summary.row_count,
                    json.dumps(summary.columns, ensure_ascii=False),
                    json.dumps([sheet.model_dump() for sheet in sheets], ensure_ascii=False),
                    summary.created_at,
                ),
            )

            rows: list[tuple[str, str, str, int, str]] = []
            for sheet_name, frame in frames.items():
                columns = [str(column).strip() for column in frame.columns]
                frame = frame.copy()
                frame.columns = columns
                for position, (_, row) in enumerate(frame.iterrows(), start=2):
                    payload = {column: _json_value(row[column]) for column in columns}
                    fingerprint = json.dumps(
                        [dataset_id, sheet_name, position, payload],
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    record_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
                    payload.update(
                        _record_id=record_id,
                        _sheet=sheet_name,
                        _source_row=position,
                    )
                    rows.append(
                        (
                            record_id,
                            dataset_id,
                            sheet_name,
                            position,
                            json.dumps(payload, ensure_ascii=False, default=str),
                        )
                    )
            connection.executemany(
                """
                INSERT INTO records
                    (record_id, dataset_id, sheet_name, row_number, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
        return summary

    def current_dataset(self) -> DatasetSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM datasets ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self._summary_from_row(row) if row else None

    def get_dataset(self, dataset_id: str) -> DatasetSummary:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone()
        if row is None:
            raise DatasetError("Датасет не найден")
        return self._summary_from_row(row)

    def list_records(
        self,
        dataset_id: str,
        *,
        limit: int = 25,
        offset: int = 0,
        query: str | None = None,
    ) -> RecordPage:
        dataset = self.get_dataset(dataset_id)
        where = "dataset_id = ? AND sheet_name = ?"
        params: list[Any] = [dataset_id, dataset.active_sheet]
        if query:
            where += " AND lower(payload_json) LIKE ?"
            params.append(f"%{query.lower()}%")

        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM records WHERE {where}", params
            ).fetchone()["total"]
            rows = connection.execute(
                f"""
                SELECT payload_json FROM records
                WHERE {where}
                ORDER BY row_number
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return RecordPage(
            items=[json.loads(row["payload_json"]) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> DatasetSummary:
        return DatasetSummary(
            dataset_id=row["id"],
            filename=row["filename"],
            active_sheet=row["active_sheet"],
            row_count=row["row_count"],
            columns=json.loads(row["columns_json"]),
            sheets=[DatasetSheet(**item) for item in json.loads(row["sheets_json"])],
            created_at=row["created_at"],
        )

