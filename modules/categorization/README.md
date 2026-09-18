# Категоризация: локальная модель и fail-closed fallback

`model.pkl` обучен на реальном комплектном Excel и поставляется в Git. Веса SentenceTransformer остаются вне Git. При наличии точных runtime-версий и локального snapshot API лениво переходит из `uninitialized` в `ready`. При отсутствии артефакта, весов или совместимой среды остаётся безопасный ручной fallback. Обучение и скачивание весов внутри HTTP-запроса отсутствуют.

## Результат проверки артефактов

Проверено 19.09.2026:

- Артефакт обучен в отдельном `.venv-categorization` на Python 3.11.16 и pinned-зависимостях из `requirements.txt`.
- Использован snapshot `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` revision `4328cf26390c98c5e3c738b4460a05b95f4911f5`; скачаны только config, tokenizer, pooling и `model.safetensors`, без ONNX/OpenVINO/TensorFlow и дубля `pytorch_model.bin`.
- `model.pkl` занимает 21 445 345 байт, `model.json` — 143 454 байт; артефакт подходит для обычного Git, LFS не требуется.

Исторические notebook-метрики: fallback 39.1%, accuracy автоматических ответов 0.5066, macro-F1 автоматических ответов 0.4034. «Business Macro-F1» 0.4690 рассчитан **только на принятых моделью ответах с истинной категорией из top-15**, а не на всех обращениях. Эти цифры не являются результатом нынешнего API или нового скрипта. Новый split и очистка отличаются, поэтому совпадение с ними не ожидается.

## Реальные holdout-метрики

- accuracy на всём holdout: 0,4931; macro-F1: 0,3887;
- coverage: 68,14% (246 из 361); fallback rate: 31,86%;
- accuracy на автоматических ответах: 0,5081; macro-F1: 0,3982;
- наиболее проблемные target-классы по holdout F1: `Проблема с ОПС/доставкой` — 0,0000, `Ошибки загрузки страницы/зависания` — 0,1176, `Письма\\бандероли` — 0,1429, `Личный кабинет` — 0,1538.

Качество низкое: `ready` означает только техническую доступность. Модель не следует использовать для автономных решений; существующие пороги и ручную проверку нужно сохранить. Полные per-class-метрики хранятся в `model.json`.

## Общий контракт признаков

`features.py` используется обучением и инференсом:

```text
clean_text(description) + " | " + service + " " + component
```

Пустые метаданные заменяются на `Не указано` одинаково в обоих путях. Версия контракта — `description-service-component-v1`; ожидается 768 признаков, ненормализованные эмбеддинги фиксированной ревизии `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`.

В API добавлено необязательное поле `component`. Существующая форма его не передаёт, поэтому используется `Не указано`; влияние этого пропуска на качество ещё не оценено. `subject` и `priority` не входят в признаки категоризатора, поскольку notebook на них не обучался. Результат работ, линия решения и SLA не используются.

## Проверка данных без тяжёлых зависимостей

Из корня репозитория:

```bash
.venv/bin/python -m modules.categorization.train_categorizer --check-data
```

Источник по умолчанию — уже имеющийся `modules/routing/data/raw/Обращения_1931.xlsx` (только чтение). Можно указать `--input /path/to/data.xlsx` или CSV с исходными колонками `Описание 2`, `Услуга`, `Компонент услуги 1 уровня`, `Вид запроса`. Аналитический `data/final.csv` имеет другую схему и не принимается молча.

Проверенный SHA-256 источника: `daf742da6bb5902149585a05a3e38a2c655347cf6001f9cae640e1f27069ac40`.
Из 1931 строки после удаления пустых описаний/меток, конфликтующих меток у одинаковых признаков (69 строк) и повторов осталось 1801. Train/test: 1440/361; все обучающие классы достаточны для трёхкратной калибровки. Top-15 выбираются только по train. Одинаковые нормализованные признаки не пересекают train/test; близкие по смыслу дубликаты отдельно не выявляются.

## Отдельное обучение

`requirements.txt` фиксирует целевые версии прямых зависимостей для отдельной среды **Python 3.11**. Не устанавливайте её поверх основной `.venv`.

После согласования установки тяжёлых зависимостей и получения весов создайте среду:

```bash
python3.11 -m venv .venv-categorization
.venv-categorization/bin/python -m pip install -r modules/categorization/requirements.txt
.venv-categorization/bin/python -m pip install -e '.[dev]'
```

Скачайте только необходимые файлы pinned snapshot в локальный Hugging Face cache:

```bash
.venv-categorization/bin/hf download sentence-transformers/paraphrase-multilingual-mpnet-base-v2 \
  1_Pooling/config.json config.json config_sentence_transformers.json model.safetensors \
  modules.json sentence_bert_config.json sentencepiece.bpe.model special_tokens_map.json \
  tokenizer.json tokenizer_config.json \
  --revision 4328cf26390c98c5e3c738b4460a05b95f4911f5 --max-workers 4
```

Скрипт обучения сам ничего не скачивает. Он принимает только 40-символьный commit SHA; `main` не принимается:

```bash
.venv-categorization/bin/python -m modules.categorization.train_categorizer \
  --embedder-revision 4328cf26390c98c5e3c738b4460a05b95f4911f5 \
  --output /tmp/model-reproduced.pkl
```

Скрипт фиксирует seed 42, CPU, один поток, split 80/20 и параметры RandomForest (400 деревьев, depth 20, leaf 3, balanced) + isotonic CV=3. Сохраняет bundle `model.pkl` и читаемый `model.json`: SHA-256 данных, хеши признаков train/test, классы, параметры, ревизию эмбеддера, версии библиотек и всей среды, полные holdout-метрики и долю fallback. Для повторения используйте тот же источник, snapshot, Python и версии из manifest; битовое совпадение между разными платформами не гарантируется. Существующий output не перезаписывается.

Проверены и unit-механика с mock-эмбеддером, и полное обучение/загрузка реального артефакта.

Для запуска API с категоризацией используйте эту же среду:

```bash
.venv-categorization/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

## Подключение и статусы

`ClassifierAdapter` подключён к общему `AnalysisService`. `TicketCategorizer` при создании проверяет наличие файла, но не импортирует transformer и не загружает веса. Первая классификация лениво загружает доверенный bundle и локальный эмбеддер под lock. Файл по умолчанию расположен рядом с `service.py`, независимо от текущего каталога. Путь и модель не принимаются через HTTP или импорт Excel.

После отдельного обучения установите одинаковые версии библиотек в среде API и перезапустите приложение. Артефакты pickle/joblib должны поступать только из доверенного обучения. Старый голый sklearn pickle без manifest отвергается: его размерность сама по себе не доказывает совместимость признаков. Версии runtime-библиотек проверяются точно, дополнительно проверяются контракт, snapshot SHA, классы, размерность и вероятности.

`GET /api/integrations`, поле `classification`:

| Статус | Значение |
|---|---|
| `model-missing` | Нет классификатора, только ручной разбор |
| `uninitialized` | Файл найден, ещё не загружен и не проверен |
| `ready` | Артефакт и локальный эмбеддер загружены |
| `model-incompatible` | Повреждение, старый формат, другие версии/признаки |
| `embedder-unavailable` | Нет доступного локального эмбеддера или размерность не совпадает |
| `inference-error` | Ошибка вычислений; последующие обращения получают fallback |

Ошибки загрузки не повторяются на каждом запросе; после исправления нужен перезапуск. Низкая уверенность (<0.25) и класс `Прочее / Неопределено` возвращают пустую категорию с ручной проверкой. Общий `AnalysisService` дополнительно сохраняет существующий порог ручной проверки 0.5. `ready` обозначает техническую доступность, не подтверждённую точность или готовность к автономным решениям.

Официальные контракты: [SentenceTransformer: local_files_only и revision](https://sbert.net/docs/package_reference/sentence_transformer/model.html), [совместимость сохранённых sklearn-моделей](https://scikit-learn.org/stable/model_persistence.html).
