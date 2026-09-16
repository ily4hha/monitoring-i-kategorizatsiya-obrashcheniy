from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models import AppealAnalysis, AppealInput, DatasetSummary, IntegrationStatus, RecordPage
from app.services.dataset_store import DatasetError, DatasetStore
from app.services.integrations import AnalysisService


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
RUNTIME_DIR = BASE_DIR / "data" / "runtime"

app = FastAPI(
    title="Мониторинг и категоризация обращений",
    version="0.1.0",
    description="API общего приложения команды хакатона.",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
dataset_store = DatasetStore(RUNTIME_DIR)
analysis_service = AnalysisService()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/integrations", response_model=IntegrationStatus)
def integrations() -> IntegrationStatus:
    return analysis_service.status()


@app.post("/api/datasets", response_model=DatasetSummary, status_code=201)
def upload_dataset(file: UploadFile = File(...)) -> DatasetSummary:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Имя файла отсутствует")
    try:
        return dataset_store.import_excel(file.file, file.filename)
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/datasets/current", response_model=DatasetSummary | None)
def current_dataset() -> DatasetSummary | None:
    return dataset_store.current_dataset()


@app.get("/api/datasets/{dataset_id}/records", response_model=RecordPage)
def records(
    dataset_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=500),
) -> RecordPage:
    try:
        return dataset_store.list_records(dataset_id, limit=limit, offset=offset, query=q)
    except DatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/records/{record_id}")
def record(record_id: str) -> dict:
    item = dataset_store.get_record(record_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    return item


@app.post("/api/appeals/analyze", response_model=AppealAnalysis)
def analyze_appeal(appeal: AppealInput) -> AppealAnalysis:
    return analysis_service.analyze(appeal)


@app.get("/api/analytics/overview")
def analytics_overview() -> dict:
    dataset = dataset_store.current_dataset()
    if dataset is None:
        return {
            "status": "no-data",
            "total_appeals": 0,
            "overdue_share": None,
            "message": "Загрузите Excel, чтобы построить аналитику.",
        }
    return {
        "status": "integration-pending",
        "total_appeals": dataset.row_count,
        "overdue_share": None,
        "message": "Базовый импорт готов. Расчёты SLA подключит модуль участника 3.",
    }

