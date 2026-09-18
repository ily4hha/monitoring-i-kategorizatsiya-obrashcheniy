from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO, Iterator

import pandas as pd

from app.models import DatasetSheet, DatasetSummary, RecordPage
from app.services.excel_reader import DatasetError, ExcelLimits, read_workbook, validate_xlsx_archive


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_XLSX_BYTES = 100 * 1024 * 1024
MAX_SHEETS = 20
MAX_ROWS_PER_SHEET = 50_000
MAX_COLUMNS_PER_SHEET = 200
MAX_CELLS_PER_SHEET = 1_000_000
MAX_ROWS_PER_WORKBOOK = 100_000
MAX_CELLS_PER_WORKBOOK = 2_000_000
MAX_SPARSE_RATIO = 100
RESERVED_COLUMNS = {"_record_id", "_sheet", "_source_row"}
ALLOWED_SUFFIXES = {".xlsx", ".xls"}
INSERT_BATCH_SIZE = 500


def _excel_limits() -> ExcelLimits:
    return ExcelLimits(
        sheets=MAX_SHEETS, rows=MAX_ROWS_PER_SHEET, columns=MAX_COLUMNS_PER_SHEET,
        cells=MAX_CELLS_PER_SHEET, sparse_ratio=MAX_SPARSE_RATIO,
        book_rows=MAX_ROWS_PER_WORKBOOK, book_cells=MAX_CELLS_PER_WORKBOOK,
        uncompressed_bytes=MAX_UNCOMPRESSED_XLSX_BYTES,
    )


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^\w.() -]+", "_", Path(filename).name, flags=re.UNICODE)
    # Filesystem limits apply to bytes, including the dataset id prefix.
    return cleaned.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "dataset.xlsx"


class DatasetStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.upload_dir = runtime_dir / "uploads"
        self.db_path = runtime_dir / "app.db"
        self._import_lock = threading.Lock()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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

    @staticmethod
    def _validate_xlsx_archive(content: bytes) -> None:
        try:
            validate_xlsx_archive(content, _excel_limits())
        except zipfile.BadZipFile as exc:
            raise DatasetError("Повреждённый XLSX-файл") from exc

    @staticmethod
    def _validate_columns(sheet_name: str, columns: list[str]) -> list[str]:
        if len(columns) > MAX_COLUMNS_PER_SHEET:
            raise DatasetError(f"Лист «{sheet_name}» превышает ограничение по размеру")
        if not columns:
            raise DatasetError(f"Лист «{sheet_name}» не содержит заголовков")
        duplicates = sorted(column for column, count in Counter(columns).items() if count > 1)
        if duplicates:
            raise DatasetError(f"На листе «{sheet_name}» есть повторяющиеся заголовки: {', '.join(duplicates)}")
        reserved = sorted(set(columns) & RESERVED_COLUMNS)
        if reserved:
            raise DatasetError(f"Служебные имена колонок недопустимы: {', '.join(reserved)}")
        return columns

    @staticmethod
    def _validate_frame(sheet_name: str, frame: pd.DataFrame) -> list[str]:
        columns = DatasetStore._validate_columns(sheet_name, [str(column).strip() for column in frame.columns])
        rows, width = len(frame), len(columns)
        if rows > MAX_ROWS_PER_SHEET or rows * width > MAX_CELLS_PER_SHEET:
            raise DatasetError(f"Лист «{sheet_name}» превышает ограничение по размеру")
        if rows == 0:
            return columns
        nonempty = int(frame.replace("", pd.NA).notna().sum().sum())
        if nonempty == 0:
            raise DatasetError(f"Лист «{sheet_name}» не содержит данных")
        if rows * width > nonempty * MAX_SPARSE_RATIO:
            raise DatasetError(f"Лист «{sheet_name}» слишком разрежен")
        return columns

    def _insert_sheet(self, connection, dataset_id, sheet_name, source_rows) -> DatasetSheet | None:
        header = next(source_rows, ())
        if not header:
            return None
        columns = self._validate_columns(sheet_name, [
            f"Unnamed: {index}" if value is None or value == "" else str(value).strip()
            for index, value in enumerate(header)
        ])
        limits = _excel_limits()
        batch = []
        row_count = nonempty = pending_empty = 0

        def append_record(position, values):
            payload = dict(zip(columns, values, strict=True))
            fingerprint = json.dumps([dataset_id, sheet_name, position, payload], ensure_ascii=False, sort_keys=True, default=str)
            record_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
            payload.update(_record_id=record_id, _sheet=sheet_name, _source_row=position)
            batch.append((record_id, dataset_id, sheet_name, position, json.dumps(payload, ensure_ascii=False, default=str)))
            if len(batch) >= INSERT_BATCH_SIZE:
                flush()

        def flush():
            connection.executemany(
                "INSERT INTO records (record_id, dataset_id, sheet_name, row_number, payload_json) VALUES (?, ?, ?, ?, ?)",
                batch,
            )
            batch.clear()

        for position, values in enumerate(source_rows, start=2):
            # Defense in depth for actual reader output; no DataFrame is built.
            limits.check_shape(sheet_name, position, len(values))
            converted = tuple(_json_value(value) for value in values)
            populated = sum(value is not None for value in converted)
            if not populated:
                pending_empty += 1
                continue
            for empty_position in range(position - pending_empty, position):
                append_record(empty_position, (None,) * len(columns))
            pending_empty = 0
            append_record(position, converted)
            row_count = position - 1
            nonempty += populated
        if not row_count:
            return None
        if row_count * len(columns) > nonempty * MAX_SPARSE_RATIO:
            raise DatasetError(f"Лист «{sheet_name}» слишком разрежен")
        flush()
        return DatasetSheet(name=sheet_name, row_count=row_count, columns=columns)

    def import_excel(self, file: BinaryIO, filename: str) -> DatasetSummary:
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise DatasetError("Поддерживаются только файлы .xlsx и .xls")
        content = file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise DatasetError("Файл превышает ограничение 25 МБ")
        if not content:
            raise DatasetError("Загружен пустой файл")

        if zipfile.is_zipfile(BytesIO(content)):
            # Keep archive-bomb validation ahead of extension and idempotency
            # checks so a renamed repeat upload cannot bypass it.
            self._validate_xlsx_archive(content)
            if suffix != ".xlsx":
                raise DatasetError("Содержимое XLSX не соответствует расширению файла .xls")
        elif content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and suffix != ".xls":
            raise DatasetError("Содержимое XLS не соответствует расширению файла .xlsx")

        with self._import_lock:
            existing = self._find_existing_dataset(content)
            if existing is not None:
                return existing
            return self._import_content(content, filename, suffix)

    def _find_existing_dataset(self, content: bytes) -> DatasetSummary | None:
        digest = hashlib.sha256(content).digest()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM datasets ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            for stored_path in self.upload_dir.glob(f"{row['id']}-*"):
                try:
                    if stored_path.stat().st_size != len(content):
                        continue
                    stored_digest = hashlib.sha256(stored_path.read_bytes()).digest()
                except OSError:
                    continue
                if stored_digest == digest:
                    return self._summary_from_row(row)
        return None

    def _import_content(self, content: bytes, filename: str, suffix: str) -> DatasetSummary:
        dataset_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        stored_path = self.upload_dir / f"{dataset_id}-{_safe_filename(filename)}"
        committed = False
        try:
            with self._connect() as connection:
                with TemporaryDirectory(prefix=".import-", dir=self.runtime_dir) as temporary:
                    with read_workbook(content, suffix, _excel_limits()) as workbook:
                        # The provisional dataset and all batches stay invisible until commit.
                        connection.execute(
                            "INSERT INTO datasets VALUES (?, ?, '', 0, '[]', '[]', ?)",
                            (dataset_id, filename, created_at),
                        )
                        sheets = []
                        for sheet_name, source_rows in workbook:
                            sheet = self._insert_sheet(connection, dataset_id, sheet_name, source_rows)
                            if sheet is not None:
                                sheets.append(sheet)
                        if not sheets:
                            raise DatasetError("В книге нет непустых листов")
                        active = max(sheets, key=lambda sheet: sheet.row_count)
                        summary = DatasetSummary(
                            dataset_id=dataset_id, filename=filename, active_sheet=active.name,
                            row_count=active.row_count, columns=active.columns, sheets=sheets, created_at=created_at,
                        )
                        connection.execute(
                            "UPDATE datasets SET active_sheet=?, row_count=?, columns_json=?, sheets_json=? WHERE id=?",
                            (active.name, active.row_count, json.dumps(active.columns, ensure_ascii=False),
                             json.dumps([sheet.model_dump() for sheet in sheets], ensure_ascii=False), dataset_id),
                        )
                    staged = Path(temporary) / f"source{suffix}"
                    staged.write_bytes(content)
                    # Publish the complete file before SQLite commit. Any exception,
                    # including commit failure, rolls back rows and removes this file.
                    staged.replace(stored_path)
            committed = True
            return summary
        except DatasetError:
            raise
        except OSError as exc:
            raise DatasetError("Не удалось сохранить датасет") from exc
        except sqlite3.Error:
            raise
        except Exception as exc:
            raise DatasetError("Не удалось прочитать Excel") from exc
        finally:
            if not committed:
                stored_path.unlink(missing_ok=True)

    def current_dataset(self) -> DatasetSummary | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM datasets ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._summary_from_row(row) if row else None

    def get_dataset(self, dataset_id: str) -> DatasetSummary:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if row is None:
            raise DatasetError("Датасет не найден")
        return self._summary_from_row(row)

    def list_records(self, dataset_id: str, *, limit: int = 25, offset: int = 0, query: str | None = None) -> RecordPage:
        dataset = self.get_dataset(dataset_id)
        where = "dataset_id = ? AND sheet_name = ?"
        params = [dataset_id, dataset.active_sheet]
        with self._connect() as connection:
            if query:
                needle = query.casefold()
                connection.create_function(
                    "casefold",
                    1,
                    lambda value: "" if value is None else str(value).casefold(),
                    deterministic=True,
                )
                column_placeholders = ", ".join("?" for _ in dataset.columns)
                where += f"""
                    AND EXISTS (
                        SELECT 1
                        FROM json_each(records.payload_json) AS field
                        WHERE field.key IN ({column_placeholders})
                          AND instr(
                              casefold(
                                  CASE field.type
                                      WHEN 'true' THEN 'True'
                                      WHEN 'false' THEN 'False'
                                      WHEN 'null' THEN ''
                                      ELSE CAST(field.value AS TEXT)
                                  END
                              ),
                              ?
                          ) > 0
                    )
                """
                params.extend(dataset.columns)
                params.append(needle)
            total = connection.execute(f"SELECT COUNT(*) FROM records WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT payload_json FROM records WHERE {where} ORDER BY row_number LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return RecordPage(items=[json.loads(row["payload_json"]) for row in rows], total=total, limit=limit, offset=offset)

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM records WHERE record_id = ?", (record_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def iter_active_records(self, dataset_id: str) -> Iterator[dict[str, Any]]:
        """Yield every source record from a dataset's active sheet."""
        dataset = self.get_dataset(dataset_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM records WHERE dataset_id = ? AND sheet_name = ? ORDER BY row_number",
                (dataset_id, dataset.active_sheet),
            )
            for row in rows:
                yield json.loads(row["payload_json"])

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> DatasetSummary:
        return DatasetSummary(
            dataset_id=row["id"], filename=row["filename"], active_sheet=row["active_sheet"],
            row_count=row["row_count"], columns=json.loads(row["columns_json"]),
            sheets=[DatasetSheet(**item) for item in json.loads(row["sheets_json"])], created_at=row["created_at"],
        )
