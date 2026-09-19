from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AppealInput(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20_000)
    service: str | None = Field(default=None, max_length=500)
    component: str | None = Field(default=None, max_length=500)
    priority: str | None = Field(default=None, max_length=100)


class CategoryPrediction(BaseModel):
    category: str | None
    confidence: float = Field(ge=0, le=1)
    explanation: str
    needs_manual_review: bool
    limitation: str | None = None


class RoutingPrediction(BaseModel):
    needs_manual_review: bool = False
    support_line: str | None
    confidence: float = Field(ge=0, le=1)
    explanation: str


class SimilarAppeal(BaseModel):
    record_id: str = Field(description="SQLite ID для GET /api/records/{record_id}; не номер обращения.")
    appeal_number: str | None = None
    score: float = Field(ge=0, le=1)
    score_kind: Literal["textual_similarity"] = "textual_similarity"
    score_description: str = "Косинусное сходство TF-IDF; не вероятность правильного решения."
    category: str | None = None
    support_line: str | None = None
    resolution: str | None = None
    explanation: str | None = None


class AppealAnalysis(BaseModel):
    category: CategoryPrediction
    routing: RoutingPrediction
    similar_appeals: list[SimilarAppeal]
    manual_review_required: bool


class DatasetSheet(BaseModel):
    name: str
    row_count: int
    columns: list[str]


class DatasetSummary(BaseModel):
    dataset_id: str
    filename: str
    active_sheet: str
    row_count: int
    columns: list[str]
    sheets: list[DatasetSheet]
    created_at: str


class ImportedRecord(BaseModel):
    """Stable metadata plus arbitrary original and enriched Excel columns."""

    model_config = ConfigDict(extra="allow")
    source_record_id: str = Field(alias="_record_id")
    source_sheet: str = Field(alias="_sheet")
    source_row: int = Field(alias="_source_row", ge=2)


class RecordPage(BaseModel):
    items: list[dict[str, Any]] = Field(description="Исходные поля Excel и метаданные _record_id, _sheet, _source_row.")
    total: int
    limit: int
    offset: int


class IntegrationStatus(BaseModel):
    classification: str
    routing: str
    similarity: str
    analytics: str


class HealthStatus(BaseModel):
    status: Literal["ok"]


class AnalyticsKpis(BaseModel):
    total_appeals: int = Field(ge=0)
    overdue_count: int | None = Field(ge=0)
    overdue_share: float | None = Field(ge=0, le=1)
    mean_sla_h: float | None
    median_sla_h: float | None
    multiline_count: int | None = Field(ge=0)
    high_clarifications_count: int | None = Field(ge=0)


class DistributionItem(BaseModel):
    value: str
    count: int = Field(ge=0)
    share: float = Field(ge=0, le=1)


class AnalyticsWorkload(BaseModel):
    categories: list[DistributionItem]
    lines: list[DistributionItem]


class SlaGroupMetrics(AnalyticsKpis):
    value: str
    count: int = Field(ge=0)
    sla_sample_size: int = Field(ge=0)
    overdue_sample_size: int = Field(ge=0)
    multiline_share: float | None = Field(ge=0, le=1)
    mean_total_work_h: float | None
    mean_total_react_h: float | None


class SlaBreakdowns(BaseModel):
    services: list[SlaGroupMetrics]
    categories: list[SlaGroupMetrics]
    category_groups: list[SlaGroupMetrics]
    priorities: list[SlaGroupMetrics]
    lines: list[SlaGroupMetrics]


class HistoricalRiskGroup(BaseModel):
    value: str
    total_appeals: int = Field(ge=0)
    overdue_sample_size: int = Field(ge=0)
    overdue_count: int | None = Field(ge=0)
    overdue_share: float | None = Field(ge=0, le=1)
    overall_overdue_share: float | None = Field(ge=0, le=1)
    overdue_share_delta: float | None = Field(ge=-1, le=1)
    overdue_risk_ratio: float | None = Field(ge=0)
    minimum_group_size: int = Field(ge=1)
    is_reliable: bool
    is_elevated_historical_risk: bool
    status: Literal["insufficient-sample", "elevated", "not-elevated"]
    reliability_message: str


class HistoricalSlaRisk(BaseModel):
    definition: str = Field(description="Историческое сравнение долей просрочки, не прогноз.")
    minimum_group_size: int = Field(ge=1)
    overall_overdue_share: float | None = Field(ge=0, le=1)
    categories: list[HistoricalRiskGroup]
    lines: list[HistoricalRiskGroup]


class AnalyticsFilters(BaseModel):
    date_from: str | None
    date_to: str | None
    service: str | None
    category: str | None
    priority: str | None
    line: str | None


class AnalyticsSampleSizes(BaseModel):
    source_records: int = Field(ge=0)
    filtered_records: int = Field(ge=0)
    sla_records: int = Field(ge=0)
    overdue_records: int = Field(ge=0)
    multiline_records: int = Field(ge=0)
    clarification_records: int = Field(ge=0)


class AnalyticsOverview(AnalyticsKpis):
    """Same shape for ready, partial and empty data; null means no observations."""

    status: Literal["ready", "partial-data", "no-data"]
    kpis: AnalyticsKpis
    category_distribution: list[DistributionItem]
    line_distribution: list[DistributionItem]
    workload: AnalyticsWorkload
    sla_breakdowns: SlaBreakdowns
    historical_sla_risk: HistoricalSlaRisk
    applied_filters: AnalyticsFilters
    sample_sizes: AnalyticsSampleSizes
    source_size: int = Field(ge=0)
    sample_size: int = Field(ge=0)
    missing_columns: list[str]
    message: str
