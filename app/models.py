from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
    record_id: str
    score: float = Field(ge=0, le=1)
    category: str | None = None
    support_line: str | None = None
    resolution: str | None = None


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


class RecordPage(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class IntegrationStatus(BaseModel):
    classification: str
    routing: str
    similarity: str
    analytics: str
