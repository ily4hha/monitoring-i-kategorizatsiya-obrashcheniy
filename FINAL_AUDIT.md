# Финальный release-аудит

Дата проверки: 19.09.2026 (Europe/Moscow)  
Ветка: `feature/final-integration`  
Аудируемый SHA приложения: `8ad59e9d3545290f53e4082ee49c664d8a7b0151`  
Удалённая ветка на момент аудита: `origin/feature/final-integration` = `d05feeb`; локальная ветка была впереди на 2 коммита.  
Платформа проверки: macOS arm64.

## Вердикт

**READY** для Merge Request и операторского/демонстрационного использования с сохранением ручной проверки.

Release-blocker не обнаружен. Полный набор тестов прошёл, FastAPI и основной HTTP-сценарий работают, реальный классификатор достиг статуса `ready` в отдельной воспроизводимой Python 3.11 ML-среде, а отсутствие ML-артефакта/окружения приводит к честному fail-closed fallback.

`READY` не означает готовность моделей к автономному принятию решений. Качество категоризации низкое, routing confidence не калиброван, а содержательная полезность похожих обращений ограничена. Флаги ручной проверки и пороги нельзя отключать.

## Git и состав релиза

Исходная проверка:

```bash
git branch --show-current
git rev-parse HEAD
git status --short --branch
git diff --check
git log --format='%H %s' origin/feature/final-integration..HEAD
```

Фактический результат:

- ветка `feature/final-integration`;
- HEAD `8ad59e9d3545290f53e4082ee49c664d8a7b0151` до добавления этого отчёта;
- рабочее дерево было чистым;
- `git diff --check` не вывел ошибок;
- локально находились два ещё не опубликованных коммита: `a69eed9` и `8ad59e9`.

Проверка отсутствия runtime-мусора среди tracked-файлов:

```bash
git ls-files | awk '/(^|\/)(\.venv|venv|__pycache__|\.pytest_cache|uploads|runtime|artifacts)(\/|$)|\.(sqlite|sqlite3|db)(-|$)/ {print}'
git check-ignore -v \
  data/runtime/app.db data/runtime/uploads/placeholder \
  .venv/bin/python .venv-categorization/bin/python \
  __pycache__/x.pyc artifacts/x
```

Первый поиск вернул пустой результат: SQLite, uploads, venv, cache и runtime-каталоги в Git не отслеживаются. Второй подтвердил правила `.gitignore` для `runtime/`, `.venv/`, `.venv-categorization/`, `__pycache__/` и `artifacts/`. Файлы `modules/categorization/model.pkl` и `modules/routing/reports/routing/assistant.joblib` являются намеренно поставляемыми model artifacts, а не runtime-мусором.

Контрольные SHA-256 и размеры артефактов:

| Файл | SHA-256 | Размер |
|---|---|---:|
| `modules/categorization/model.pkl` | `e431d2bec65558e443c46eac5f70e45b686d753c274fc1dbc81579ac65560b00` | 21 445 345 B |
| `modules/categorization/model.json` | `91fc2b44f7f2d8aa72278d7744ab65f72a8ae62b1f338dc33a77d8569ad0465c` | 143 454 B |
| `modules/routing/reports/routing/assistant.joblib` | `e276eea5c8e2a5f8ed19a08e15669886a9f022bd37ac205b126c02468e65a9ec` | 3 968 847 B |
| `modules/routing/data/raw/Обращения_1931.xlsx` | `daf742da6bb5902149585a05a3e38a2c655347cf6001f9cae640e1f27069ac40` | 743 372 B |

## Чистая установка приложения и полный pytest

Использовалось новое временное окружение, а не репозиторная `.venv`:

```bash
python3 -m venv /private/tmp/postcode-final-audit-app.blZlfx/venv
/private/tmp/postcode-final-audit-app.blZlfx/venv/bin/python -m pip install --upgrade pip
/private/tmp/postcode-final-audit-app.blZlfx/venv/bin/python -m pip install -e '.[dev]'
/private/tmp/postcode-final-audit-app.blZlfx/venv/bin/python -m pytest -ra
```

Фактический результат:

- Python 3.14.6, что соответствует заявленному `requires-python = ">=3.11"`;
- чистая установка завершена успешно;
- `126 passed, 39442 warnings in 12.32s`;
- skipped/xfail отсутствуют;
- предупреждения: deprecation в связке Starlette/TestClient и массовое предупреждение joblib о `array.shape` на NumPy 2.5; функциональных ошибок нет.

Первый sandbox-прогон дал `123 passed`, 1 failure и 2 errors, потому что среда запрещала `socket.bind(("127.0.0.1", 0))`. Полный набор был повторён вне сетевого sandbox; все три live-HTTP теста прошли. Это ограничение среды аудита, а не дефект репозитория.

## Запуск FastAPI и живой HTTP-сценарий

Обычная среда приложения:

```bash
/private/tmp/postcode-final-audit-app.blZlfx/venv/bin/python -m uvicorn \
  app.main:create_app --factory --host 127.0.0.1 --port 8765
curl --fail --silent --show-error http://127.0.0.1:8765/api/health
curl --fail --silent --show-error http://127.0.0.1:8765/api/integrations
curl --fail --silent --show-error -X POST http://127.0.0.1:8765/api/appeals/analyze \
  -H 'Content-Type: application/json' \
  -d '{"subject":"Не приходит письмо","description":"Письмо зависло в очереди и не доставляется адресату","service":"Корпоративная почта","component":"Доставка","priority":"Высокий"}'
```

Фактический результат:

- Uvicorn: `Application startup complete`;
- `/api/health`: HTTP 200, `{"status":"ok"}`;
- routing, similarity, analytics: `ready`;
- обычная среда не содержит тяжёлого `sentence-transformers`, поэтому первая попытка категоризации безопасно дала `model-incompatible`, пустую категорию, confidence 0 и `manual_review_required=true`;
- routing вернул `(1 линия)`, confidence `0.7366579888`;
- similarity вернул ровно 5 обращений с `record_id`, score, категорией, линией и результатом;
- общий запрос анализа завершился HTTP 200, несмотря на недоступную категоризацию.

## Excel, список и карточка обращения

```bash
curl --fail --silent --show-error -X POST http://127.0.0.1:8765/api/datasets \
  -F 'file=@modules/routing/data/raw/Обращения_1931.xlsx'
curl --fail --silent --show-error http://127.0.0.1:8765/api/datasets/current
curl --fail --silent --show-error \
  'http://127.0.0.1:8765/api/datasets/ab8ab91c60684f23847d553ce938e058/records?limit=2&offset=0'
curl --fail --silent --show-error \
  http://127.0.0.1:8765/api/records/68b70e4f58f5ca96ae32
```

Фактический результат:

- POST `/api/datasets`: HTTP 201;
- файл `Обращения_1931.xlsx`, лист `Sheet0`, 1 931 строка, 42 колонки;
- `/api/datasets/current`: тот же активный датасет;
- список: HTTP 200, `total=1931`, пагинация `limit=2`, устойчивые `_record_id`;
- карточка `68b70e4f58f5ca96ae32`: HTTP 200, исходная строка 2, номер обращения `37811141` и все исходные поля сохранены.

`dataset_id` и `_record_id` детерминированы содержимым/строкой, но приведённые значения относятся именно к этому прогону.

## SLA

В комплектном сыром Excel нет производных полей `fact_sla_h`, `is_overdue`, `category_grouped`, `resolved_line`. Поэтому живой API корректно вернул:

- `status=partial-data`;
- `total_appeals=1931`;
- все четыре отсутствующие колонки в `missing_columns`;
- сообщение о неполных данных вместо фиктивных SLA-значений.

Полная формула проверена живым API на репозиторном fixture, временно преобразованном в XLSX:

```bash
/private/tmp/postcode-final-audit-app.blZlfx/venv/bin/python -c \
  "import pandas as pd; pd.read_csv('tests/fixtures/sla_analytics.csv').to_excel('/private/tmp/postcode-final-audit-sla.xlsx', index=False)"
curl --fail --silent --show-error -X POST http://127.0.0.1:8765/api/datasets \
  -F 'file=@/private/tmp/postcode-final-audit-sla.xlsx'
curl --fail --silent --show-error http://127.0.0.1:8765/api/analytics/overview
```

Результат: `status=ready`, 4 обращения, 2 просроченных, `overdue_share=0.5`, mean SLA `4.0` ч, median `3.0` ч, заполненные распределения категорий и линий, `missing_columns=[]`.

## Настоящая категоризация: `ready`

Проверена отдельная чистая ML-среда на Python 3.11.16:

```bash
/Users/ilya/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 \
  -m venv /private/tmp/postcode-final-audit-ml.ekpu2z/venv
/private/tmp/postcode-final-audit-ml.ekpu2z/venv/bin/python -m pip install --upgrade pip
/private/tmp/postcode-final-audit-ml.ekpu2z/venv/bin/python -m pip install \
  -r modules/categorization/requirements.txt
/private/tmp/postcode-final-audit-ml.ekpu2z/venv/bin/python -m pip install -e '.[dev]'
/private/tmp/postcode-final-audit-ml.ekpu2z/venv/bin/python -m pip check
/private/tmp/postcode-final-audit-ml.ekpu2z/venv/bin/python \
  -m modules.categorization.train_categorizer --check-data
```

Фактический результат:

- чистая установка завершена успешно, `pip check`: `No broken requirements found`;
- точные версии: pandas 2.3.2, NumPy 2.2.6, scikit-learn 1.7.2, sentence-transformers 5.1.1, transformers 4.56.2, torch 2.8.0, openpyxl 3.1.5, joblib 1.5.2;
- preflight данных: SHA-256 `daf742…ac40`, 1 931 исходная строка, 1 801 подготовленная, 69 конфликтующих строк исключено, train 1 440, test 361, seed 42;
- реальный `model.pkl` и локальный pinned SentenceTransformer snapshot загрузились без сети;
- статус изменился `uninitialized -> ready`;
- контрольный текст про отсутствующие SMS/push был классифицирован как `Проблема с push/sms/email`, confidence `0.4657836285`, `is_reliable=true`;
- через живой FastAPI на порту 8766 `/api/integrations` после inference вернул `classification=ready`, routing/similarity/analytics также `ready`.

Snapshot эмбеддера намеренно не находится в Git. Использовался локально доступный `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` revision `4328cf26390c98c5e3c738b4460a05b95f4911f5`. Точная команда его загрузки с перечнем файлов и immutable revision приведена в `modules/categorization/README.md`.

## Fallback при недоступном артефакте

Проверено без перемещения поставляемого артефакта: `TicketCategorizer` запущен с заведомо отсутствующим `/private/tmp/postcode-final-audit-absent/model.pkl`.

Результат:

- `/api/integrations`: `classification=model-missing`;
- анализ: HTTP 200;
- `category=null`, confidence `0.0`, `needs_manual_review=true`;
- limitation: `Категоризация недоступна: model-missing`;
- `manual_review_required=true`;
- тяжёлый embedder не загружался и повторная попытка обучения/скачивания в HTTP-пути не выполнялась.

Поведение дополнительно покрыто тестами для отсутствующего файла, отсутствующего/несовпадающего metadata, повреждённого pickle, несовместимых версий, отсутствующего локального embedder и inference error.

## Метрики моделей

### Категоризация

Источник: `modules/categorization/model.json`, holdout 361 обращение.

| Метрика | Значение |
|---|---:|
| Holdout accuracy | 0.4931 |
| Holdout macro-F1 | 0.3887 |
| Coverage | 0.6814 (246/361) |
| Fallback rate | 0.3186 |
| Accuracy на автоматических ответах | 0.5081 |
| Macro-F1 на автоматических ответах | 0.3982 |

Критичные слабые классы: `Проблема с ОПС/доставкой` F1 0.0000, `Ошибки загрузки страницы/зависания` 0.1176, `Письма\бандероли` 0.1429, `Личный кабинет` 0.1538. Это не допускает автономной категоризации.

### Routing и поиск похожих

Источник: `modules/routing/reports/routing/metrics.json`, test 271 обращение.

| Метрика | Значение |
|---|---:|
| Routing accuracy | 0.6937 |
| Routing macro-F1 по представленным линиям | 0.4288 |
| Majority baseline accuracy | 0.6273 |
| Automatic coverage при пороге 0.85 | 0.3432 |
| Accuracy принятых рекомендаций | 0.8172 |
| Similarity category hit@5 | 0.6642 |
| Similarity line hit@5 | 0.9114 |
| Запросы без результатов | 0/271 |

## Ограничения и риски

- `classification=ready` означает техническую доступность, а не достаточное качество. Общий порог ручной проверки 0.5 сохраняется.
- Без точного Python 3.11 ML-окружения и локального pinned HF snapshot обычное приложение намеренно работает в fallback. Snapshot нужно отдельно provision-ить на каждом runtime-хосте.
- Pickle/joblib загружаются только как доверенные локальные артефакты; нельзя подменять их пользовательской загрузкой.
- Routing confidence не калиброван. Линия 4 представлена одной test-записью и отсутствует в train, поэтому модель её не выбирает.
- Похожие обращения ищутся во встроенном train-индексе, а не в загруженном Excel; их ID нельзя открывать через API карточек импортированного датасета.
- Ручная содержательная оценка routing-модуля: только 4/10 первых результатов тематически релевантны и 0/10 содержат конкретные шаги решения. Hit@5 — косвенная метрика.
- Комплектный сырой Excel демонстрирует импорт, но не содержит производной SLA-схемы; полный SLA требует подготовленных колонок.
- Зависимости приложения заданы диапазонами, поэтому чистая установка уже показывает deprecation warnings на новых FastAPI/Starlette/NumPy. Сейчас тесты зелёные, но CI стоит выполнять на каждом dependency update.
- Битовое совпадение повторного обучения между платформами не гарантируется; воспроизводимы данные, split, seed, версии, feature contract и immutable revision эмбеддера.

## Действия владельца перед MR

1. Просмотреть этот отчёт и убедиться, что MR позиционирует модели как помощь оператору, а не автономное решение.
2. Запушить локальные коммиты ветки, включая отдельный commit с `FINAL_AUDIT.md`; merge/push в рамках аудита не выполнялись.
3. В описании MR явно указать deployment prerequisite для `classification=ready`: Python 3.11, `modules/categorization/requirements.txt` и pinned HF snapshot revision `4328cf…1f5`; fallback без snapshot является ожидаемым.
4. Запустить MR CI на опубликованной ветке и проверить, что полный `pytest` проходит в runner с разрешённым localhost socket.
5. Не отключать `manual_review_required`, пороги и fail-closed fallback до отдельного улучшения качества моделей и экспертной приёмки.
