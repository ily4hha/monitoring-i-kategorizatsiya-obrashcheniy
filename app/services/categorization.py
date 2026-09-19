"""Adapter for the bundled offline categorization model and manual fallback."""
from app.models import AppealInput, CategoryPrediction


class ClassifierAdapter:
    def __init__(self, categorizer=None):
        if categorizer is not None:
            self.categorizer = categorizer
            self.categorizer.start()
            return
        try:
            # Keep ``app`` importable as a standalone package. The integration module is
            # optional and the HTTP application must still start when it is absent.
            from modules.categorization.service import TicketCategorizer
        except ModuleNotFoundError:
            self.categorizer = None
        else:
            self.categorizer = TicketCategorizer()
            self.categorizer.start()

    @property
    def status(self):
        return self.categorizer.status if self.categorizer is not None else "model-missing"

    def predict(self, appeal: AppealInput) -> CategoryPrediction:
        if self.categorizer is None:
            return CategoryPrediction(
                category=None,
                confidence=0,
                explanation="Модуль категоризации отсутствует. Восстановите поставляемый модуль и артефакт; требуется ручной разбор.",
                needs_manual_review=True,
                limitation="Категоризация недоступна: model-missing",
            )
        # Only registration-time description, service and component are model features.
        result = self.categorizer.predict(appeal.description, appeal.service, appeal.component)
        return CategoryPrediction(
            category=result["category"] if result["is_reliable"] else None,
            confidence=result["confidence"], explanation=result["explanation"],
            needs_manual_review=not result["is_reliable"],
            limitation=result.get("limitation"),
        )
