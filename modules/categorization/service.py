"""
Модуль категоризации обращений (Участник 1).
Использует эмбеддинги paraphrase-multilingual-mpnet-base-v2 + калиброванный RandomForest.

Использование другими участниками:
    from service import TicketCategorizer
    categorizer = TicketCategorizer()
    result = categorizer.predict("Не могу войти в личный кабинет, выкидывает на главную")
    print(result)
    # {'category': 'Личный кабинет', 'confidence': 0.72, 'is_reliable': True}
"""

import re
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.calibration import CalibratedClassifierCV
import joblib
import os

DEFAULT_THRESHOLD = 0.25
EMBEDDER_NAME = 'paraphrase-multilingual-mpnet-base-v2'

class TicketCategorizer:
    def __init__(self, model_path='model.pkl', threshold=DEFAULT_THRESHOLD):
        self.threshold = threshold
        # Веса трансформера скачиваются SentenceTransformer автоматически при первом запуске.
        self.embedder = SentenceTransformer(EMBEDDER_NAME)
        
        # Если есть сохранённая модель — загружаем, иначе обучаем заглушку
        if model_path and os.path.exists(model_path):
            self.model = joblib.load(model_path)
        else:
            self.model = None
            
        self.top_15 = [
            'Отслеживание отправлений', 'Прочее', 'Проблема с QR-код(подключение/отключение)',
            'Электронные обращения', 'Проблемы в работе Препост/PrePost',
            'Ошибка в приложении (восстановление работоспособности)',
            'Формирование и закрытие емкостей', 'Проблема с push/sms/email',
            'Импорт списков из ЛК ЮЛ', 'Письма\\бандероли',
            'Импорт списков из архива ф.103 (zip-архив)',
            'Проблема с авторизацией/ЭЗП/Бонусами/Доверенностями',
            'Ошибки загрузки страницы/зависания', 'Личный кабинет',
            'Проблема с ОПС/доставкой'
        ]

    def clean_text(self, text):
        if not isinstance(text, str):
            return ""
        text = text.lower()
        text = re.sub(r'тема письма[:\s]*|текст письма[:\s]*|индекс опс[:\s]*|добрый день[,!\s]*|здравствуйте[,!\s]*', ' ', text)
        text = re.sub(r'http\S+|www\S+', ' <URL> ', text)
        text = re.sub(r'\b\d+\b', ' <NUM> ', text)
        return re.sub(r'\s+', ' ', text).strip()

    def predict(self, text, service="", component=""):
        """
        Категоризует одно обращение.
        Возвращает dict: category, confidence, is_reliable, explanation
        """
        combined = f"{self.clean_text(text)} | {service} {component}"
        embedding = self.embedder.encode([combined])
        
        if self.model is None:
            return {
                'category': 'Прочее / Неопределено',
                'confidence': 0.0,
                'is_reliable': False,
                'explanation': 'Модель не загружена. Требуется обучение.'
            }
        
        proba = self.model.predict_proba(embedding)[0]
        pred_idx = np.argmax(proba)
        confidence = float(proba[pred_idx])
        category = self.model.classes_[pred_idx]
        
        is_reliable = (confidence >= self.threshold) and (category != 'Прочее / Неопределено')
        
        return {
            'category': category if is_reliable else 'ОПЕРАТОР',
            'confidence': round(confidence, 3),
            'is_reliable': is_reliable,
            'explanation': f'Уверенность модели: {confidence:.2f}. Порог: {self.threshold}.'
        }
