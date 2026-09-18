from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models import AppealAnalysis, AppealInput, DatasetSummary, IntegrationStatus, RecordPage
from app.services.dataset_store import DatasetError, DatasetStore
from app.services.integrations import AnalysisService
from app.services.routing import RoutingAdapter, RoutingRuntime, SimilaritySearchAdapter
from app.uploads import LimitedUploadRoute


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
MAX_OFFSET = 100_000


def create_app(runtime_dir: Path = RUNTIME_DIR, *, analysis: AnalysisService | None = None) -> FastAPI:
    """Create runtime resources only when explicitly called (Uvicorn --factory)."""
    runtime = RoutingRuntime()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if analysis is None:
            runtime.start()
        yield

    application = FastAPI(
        lifespan=lifespan,
        title="Мониторинг и категоризация обращений",
        version="0.1.0",
        description="API общего приложения команды хакатона.",
    )
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    application.state.dataset_store = DatasetStore(runtime_dir)
    application.state.analysis_service = analysis if analysis is not None else AnalysisService(
        router=RoutingAdapter(runtime), similarity=SimilaritySearchAdapter(runtime),
    )
    uploads = APIRouter(route_class=LimitedUploadRoute)

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/integrations", response_model=IntegrationStatus)
    def integrations() -> IntegrationStatus:
        return application.state.analysis_service.status()

    @uploads.post("/api/datasets", response_model=DatasetSummary, status_code=201)
    def upload_dataset(file: UploadFile = File(...)) -> DatasetSummary:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Имя файла отсутствует")
        try:
            return application.state.dataset_store.import_excel(file.file, file.filename)
        except DatasetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    application.include_router(uploads)

    @application.get("/api/datasets/current", response_model=DatasetSummary | None)
    def current_dataset() -> DatasetSummary | None:
        return application.state.dataset_store.current_dataset()

    @application.get("/api/datasets/{dataset_id}/records", response_model=RecordPage)
    def records(
        dataset_id: str,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        q: str | None = Query(default=None, max_length=500),
    ) -> RecordPage:
        if offset > MAX_OFFSET:
            raise HTTPException(status_code=400, detail=f"offset не может превышать {MAX_OFFSET}")
        try:
            return application.state.dataset_store.list_records(dataset_id, limit=limit, offset=offset, query=q)
        except DatasetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/api/records/{record_id}")
    def record(record_id: str) -> dict:
        item = application.state.dataset_store.get_record(record_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Обращение не найдено")
        return item

    @application.post("/api/appeals/analyze", response_model=AppealAnalysis)
    def analyze_appeal(appeal: AppealInput) -> AppealAnalysis:
        try:
            return application.state.analysis_service.analyze(appeal)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Сервис анализа временно недоступен",
            ) from exc

    @application.get("/api/analytics/overview")
    def analytics_overview() -> dict:
        dataset = application.state.dataset_store.current_dataset()
        if dataset is None:
            return {
                "status": "no-data", "total_appeals": 0, "overdue_share": None,
                "message": "Загрузите Excel, чтобы построить аналитику.",
            }
        return {
            "status": "integration-pending", "total_appeals": dataset.row_count,
            "overdue_share": None,
            "message": "Базовый импорт готов. Расчёты SLA подключит модуль участника 3.",
        }

    return application
