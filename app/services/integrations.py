from __future__ import annotations

from typing import Protocol

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


class PendingClassifier:
    def predict(self, appeal: AppealInput) -> CategoryPrediction:
        return CategoryPrediction(
            category=None,
            confidence=0,
            explanation="Модель категоризации ещё не подключена к общему API.",
            needs_manual_review=True,
            limitation="Требуется интеграция модуля участника 1.",
        )


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
        self.classifier = classifier or PendingClassifier()
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
                or routing.confidence < 0.5
                or routing.support_line is None
            ),
        )

    @staticmethod
    def status() -> IntegrationStatus:
        return IntegrationStatus(
            classification="pending",
            routing="pending",
            similarity="pending",
            analytics="dataset-preview-ready",
        )

