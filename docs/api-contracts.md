# Согласованные API-контракты

Источники схем: `app/models.py`, `GET /openapi.json` и `/docs`.
Примеры ниже получены через HTTP TestClient с настоящими адаптерами и моделями
в основной `.venv`, после импорта синтетического сырого Excel из
`tests/test_api_contracts.py`. Они не содержат пользовательских обращений.
SQLite ID в примере относится к временной тестовой базе; после новой загрузки ID будет другим.

## POST /api/appeals/analyze

Полный JSON ответа: [analyze.response.json](contracts/analyze.response.json).

```json
{
  "subject": "Ошибка авторизации",
  "description": "Не могу войти в личный кабинет, пароль не работает",
  "service": "Личный кабинет",
  "component": "Авторизация"
}
```

| Поле ответа | Контракт |
| --- | --- |
| `category` | `category: string/null`, `confidence: number [0,1]`, `explanation: string`, `needs_manual_review: boolean`, `limitation: string/null` |
| `routing` | `support_line: string/null`, `confidence: number [0,1]`, `explanation: string`, `needs_manual_review: boolean` |
| `manual_review_required` | Общий признак ручной проверки с сохранением политики существующих адаптеров |
| `similar_appeals` | До пяти записей активного листа Excel; без совпадений — `[]` |

Каждый похожий случай содержит `record_id`, `appeal_number`, `score`,
`score_kind`, `score_description`, `category`, `support_line`, `resolution`,
`explanation`. `record_id` совпадает с `_record_id` в
`GET /api/records/{record_id}`. Номер обращения хранится отдельно в
`appeal_number`. Поля исходной записи могут быть `null`, если отсутствуют в Excel.
`score_kind` равен `textual_similarity`; `score` — косинусное сходство TF-IDF,
не вероятность правильности решения. Frontend отображает описание оценки и
совпавшие термины, открывает исходную запись по SQLite ID.

`GET /api/records/{record_id}` сохраняет все исходные и обогащённые поля Excel.
Его модель фиксирует `_record_id`, `_sheet`, `_source_row` и разрешает произвольные
дополнительные колонки. Отсутствующая запись даёт 404.

## GET /api/analytics/overview

Полный JSON ответа: [analytics.response.json](contracts/analytics.response.json).
Пример рассчитан с шестью фильтрами:

```json
{
  "date_from": "2025-01-02",
  "date_to": "2025-01-02",
  "service": "Личный кабинет",
  "category": "Проблема с авторизацией",
  "priority": "Высокий",
  "line": "2 линия"
}
```

Все фильтры необязательны и передаются query-параметрами. Даты имеют формат
`YYYY-MM-DD` и включают весь календарный день исходной записи. Неверный формат —
422, обратный диапазон — 400. Категория сопоставляется с исходным видом или
обобщённой категорией. После загрузки другой книги frontend сбрасывает фильтры
и получает новые варианты из полной выборки.

| Поле | Содержимое |
| --- | --- |
| `status` | `ready`, `partial-data` или `no-data` |
| `kpis` | `total_appeals`, `overdue_count`, `overdue_share`, `mean_sla_h`, `median_sla_h`, `multiline_count`, `high_clarifications_count` |
| `workload` | `categories`, `lines`: массивы `{value, count, share}` |
| `sla_breakdowns` | `services`, `categories`, `category_groups`, `priorities`, `lines`: количество, размеры выборок, SLA, просрочка, многолинейность, уточнения, среднее время работы и реакции |
| `historical_sla_risk` | Определение, минимум наблюдений (20), общая доля просрочки, группы `categories` и `lines` |
| `applied_filters` | Все шесть фильтров; невыбранные равны `null` |
| `sample_sizes` | `source_records`, `filtered_records`, `sla_records`, `overdue_records`, `multiline_records`, `clarification_records` |
| `missing_columns`, `message` | Недостающие аналитические поля и пояснение состояния |

Плоские KPI, `category_distribution`, `line_distribution`, `source_size` и
`sample_size` сохранены для совместимости с текущим frontend. Они совпадают
с соответствующими вложенными значениями. Структура одинакова без Excel,
при пустом результате фильтра и при частичных данных. Если наблюдений для метрики
нет, её значение равно `null`, а размер выборки — нулю.

Исторический риск сравнивается с общей долей просрочки в отфильтрованной выборке.
Для каждой группы возвращаются размер выборки, доля, разность долей, отношение,
`is_reliable`, `is_elevated_historical_risk`, `status` и `reliability_message`.
Статусы: `insufficient-sample`, `elevated`, `not-elevated`. Это историческая
статистика, не прогноз будущего SLA.

## Статусы запуска

После lifespan в стандартном окружении:

```json
{
  "classification": "ready",
  "routing": "ready",
  "similarity": "ready",
  "analytics": "ready"
}
```

Готовность поиска без Excel означает доступность компонента с пустым индексом.
Ошибка inference поиска отражается только в `similarity` как
`unavailable:inference-error`; успешный следующий поиск восстанавливает `ready`.
Артефакты классификации и маршрутизации не переобучаются при импорте Excel.
