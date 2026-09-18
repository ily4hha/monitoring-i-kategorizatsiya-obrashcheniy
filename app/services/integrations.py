from __future__ import annotations

from typing import Protocol

from app.services.categorization import ClassifierAdapter

from app.models import (
    AppealAnalysis,
    AppealInput,
    CategoryPrediction,
    IntegrationStatus,
    RoutingPrediction,
    SimilarAppeal,
)


class Classifier(Protocol):
    def predict(self, appeal: AppealInput) -> CategoryPrediction: ...


class Router(Protocol):
    def recommend(self, appeal: AppealInput, category: str | None) -> RoutingPrediction: ...


class SimilaritySearch(Protocol):
    def search(self, appeal: AppealInput, limit: int = 5) -> list[SimilarAppeal]: ...


class PendingRouter:
    def recommend(self, appeal: AppealInput, category: str | None) -> RoutingPrediction:
        return RoutingPrediction(
            support_line=None,
            confidence=0,
            explanation="Модель маршрутизации ещё не подключена к общему API.",
        )


class PendingSimilaritySearch:
    def search(self, appeal: AppealInput, limit: int = 5) -> list[SimilarAppeal]:
        return []


class AnalysisService:
    def __init__(
        self,
        classifier: Classifier | None = None,
        router: Router | None = None,
        similarity: SimilaritySearch | None = None,
    ) -> None:
        self.classifier = classifier if classifier is not None else ClassifierAdapter()
        self.router = router or PendingRouter()
        self.similarity = similarity or PendingSimilaritySearch()

    def analyze(self, appeal: AppealInput) -> AppealAnalysis:
        category = self.classifier.predict(appeal)
        routing = self.router.recommend(appeal, category.category)
        similar = self.similarity.search(appeal)
        return AppealAnalysis(
            category=category,
            routing=routing,
            similar_appeals=similar,
            manual_review_required=(
                category.needs_manual_review
                or category.confidence < 0.5
                or routing.needs_manual_review
                or routing.confidence < 0.5
                or routing.support_line is None
            ),
        )

    def status(self) -> IntegrationStatus:
        return IntegrationStatus(
            classification=getattr(self.classifier, "status", "pending"),
            routing=getattr(self.router, "status", "pending"),
            similarity=getattr(self.similarity, "status", "pending"),
            analytics="ready",
        )
