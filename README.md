# Appeal Monitoring and Categorization

A local service for importing historical appeals from Excel, analyzing new appeals, recommending a category and support line, finding similar cases, and calculating SLA analytics. The project combines FastAPI, an operator interface, SQLite storage, and bundled ML artifacts; it does not require network access to external models.

## Team and Contributions

The names below are temporary placeholders. `Participant 1`–`Participant 5` can later be replaced with the team members' GitHub usernames.

- **Participant 1** — appeal categorization: feature engineering, model training and evaluation across the 15 primary categories, confidence calibration, manual-review fallback, ML artifact packaging, and metrics documentation.
- **Participant 2** — routing and similar-appeal search: support-line recommendation model, TF-IDF retrieval of similar cases, validation datasets, metrics, reports, and integration materials.
- **Participant 3** — data preparation and SLA analytics: cleaning and normalizing 1,931 appeals, maintaining the data dictionary, calculating durations and overdue cases, and validating data quality, anomalies, and multi-line cases.
- **Participant 4** — application and integration: FastAPI service, Excel import, SQLite storage, API contracts, operator interface, integration of categorization, routing, and analytics modules, plus integration and frontend tests.
- **Participant 5** — presentation and demonstration: project presentation, quality report, demonstration script, and video walkthrough of the completed service.

## What the Service Does

- uploads `.xlsx` and `.xls` files up to 25 MB, selects the largest non-empty sheet, and stores its rows in SQLite;
- categorizes a new appeal into one of the top 15 categories from the training dataset;
- displays confidence, an explanation, and the reason for manual review;
- recommends a support line;
- finds up to five similar appeals in the active uploaded dataset;
- opens the original matched record using a stable SQLite `record_id`;
- calculates SLA, workload, and historical-risk analytics with filters;
- preserves the original Excel fields and adds normalized analytical fields.

## Requirements

- Python 3.11 or newer;
- Git;
- macOS or Linux for the virtual-environment activation commands shown below.

## Installation

Use the following commands for a clean installation:

```bash
git clone https://github.com/ily4hha/monitoring-i-kategorizatsiya-obrashcheniy.git
cd monitoring-i-kategorizatsiya-obrashcheniy
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## Running the Application

```bash
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and wait for `Application startup complete`. Interactive API documentation is available at `/docs`.

At startup, the categorizer loads the local `modules/categorization/model.pkl` and `model.json` files, while the routing module loads `modules/routing/reports/routing/assistant.joblib`. No additional runtime or model downloads are required. SQLite databases and uploaded-file copies are created in `data/runtime/`, which is excluded from Git; a clean clone starts with an empty history.

## User Flow

1. Open the **History and Analytics** tab, click **Upload Excel**, and select the bundled `modules/routing/data/raw/Обращения_1931.xlsx` file containing 1,931 rows.
2. Wait for the import to finish. The service stores the original rows, enriches them with analytical fields, and builds a similar-appeal index for the active sheet.
3. Review the history: use search, open a row, and inspect all fields from the original record.
4. Review analytics and apply filters by period, service, category, priority, and support line.
5. Open the **New Appeal** tab and enter the required subject and description; service, component, and priority are optional.
6. Review the category, confidence, explanation, recommended support line, similar cases, and overall manual-review flag.
7. Open a similar case: its `record_id` links to `GET /api/records/{record_id}` and to the same source record in the interface.

`Обращения_1931.xlsx` contains the complete demonstration history. Normalized analytical fields are calculated during import; the application does not use a separate prepared CSV at runtime.

## Categorization, Confidence, and Manual Fallback

The categorizer uses the description, service, and component. Its features are word- and character-level TF-IDF n-grams, and its classifier is a sigmoid-calibrated `LinearSVC`. The model is trained on the 15 most frequent categories; all remaining labels are grouped into a manual-review class.

`category.confidence` is the highest calibrated candidate score in the 0–1 range, not a guarantee of correctness. The explanation includes the candidate, the 0.70 threshold, and up to five words with a positive contribution to the linear score. An automatic category is returned only when the input contains enough known words, confidence meets the threshold, and the candidate is a specific top-15 category.

The service returns `category=null`, `needs_manual_review=true`, and a textual reason when:

- the model or its metadata is missing, damaged, or incompatible;
- the request contains fewer than two known meaningful words;
- confidence is below 0.70;
- the candidate belongs to a label outside the top 15;
- the candidate is the overly broad `Other` class;
- inference fails.

The overall `manual_review_required` flag also accounts for a missing or low-confidence routing result. This is fail-closed behavior: the service does not present an uncertain result as a completed automatic decision.

## Routing and Similar Appeals

Routing uses the subject and description and returns `routing.support_line`, confidence, an explanation, and a manual-review flag. Routing confidence is an internal, uncalibrated model score and must not be interpreted as a probability.

Similar-appeal search is independent of the routing artifact. After an Excel file is uploaded or activated, the service builds a TF-IDF index from the text, service, and component fields of the active dataset. `similar_appeals[].score` is cosine text similarity, not the probability of a correct solution. Each result includes a SQLite `record_id`, appeal number, category, support line, resolution, and matched terms. An empty dataset, empty input text, or the absence of shared terms produces an empty result list.

`GET /api/integrations` reports independent states for `classification`, `routing`, `similarity`, and `analytics`: `ready`, `pending`, or an explicit reason for unavailability. A routing failure does not disable search over the active dataset.

## SLA and Historical Risk

During import of the demonstration Excel file, SLA durations are converted from `H:MM[:SS]` to hours. If the original actual duration is missing, it is reconstructed as the sum of response and work time across support lines 1–4; if those values are unavailable, the metric remains `null`. The overdue flag is read from the original `Просрочен?*` field: `overdue_share = overdue_count / number of rows with a recognized overdue value`.

The overview includes the appeal count, overdue count and share, average and median SLA, multi-line cases, cases with two or more clarifications, distributions, and breakdowns by service, category, priority, and line. For an incomplete user-provided schema, all available metrics are still calculated and missing columns are listed in `missing_columns`.

Historical risk is not a forecast. For each category and line, the service compares its historical overdue share with the overall share in the current filtered dataset:

- `overdue_share_delta` — difference between the shares;
- `overdue_risk_ratio` — ratio of the group share to the overall share;
- `elevated` — the group share is above the overall share and the sample is large enough;
- groups with fewer than 20 observations receive `insufficient-sample`.

The `date_from`, `date_to`, `service`, `category`, `priority`, and `line` filters are available in the interface and as query parameters for `GET /api/analytics/overview`. Calendar-date boundaries are inclusive.

## Current Model Metrics

Categorization metrics from `modules/categorization/model.json`, based on a holdout set of 361 appeals:

- accuracy — 0.5485;
- macro-F1 — 0.4587;
- at the 0.70 threshold, 50 appeals received an automatic answer: 13.85% coverage, 40 correct answers, and 80% accepted accuracy;
- all 79 holdout examples outside the top 15 were sent for manual review.

Routing metrics from `modules/routing/reports/routing/metrics.json`, based on a test set of 271 appeals:

- accuracy — 0.6937;
- macro-F1 across supported lines — 0.4288;
- at the 0.85 threshold, automatic coverage was 34.32% and accepted accuracy was 81.72%;
- majority-baseline accuracy — 0.6273.

These metrics describe fixed holdout/test splits and do not guarantee performance on new data. No separate validated user-facing metric is claimed for similar-appeal search over the active dataset.

## API

- `POST /api/datasets` — upload an Excel file;
- `GET /api/datasets/current` — retrieve the active dataset;
- `GET /api/datasets/{dataset_id}/records` — retrieve a page of records;
- `GET /api/records/{record_id}` — open a source record;
- `POST /api/appeals/analyze` — analyze a new appeal;
- `GET /api/analytics/overview` — retrieve filtered analytics;
- `GET /api/integrations` — check module readiness;
- `GET /api/health` — check application health.

The `AppealAnalysis` and `AnalyticsOverview` contracts are published in `/openapi.json` and documented in [docs/api-contracts.md](docs/api-contracts.md). JSON examples are available in `docs/contracts/`.

## Limitations

- categorization and routing quality is limited by the training data; manual fallback is part of the normal workflow;
- categorization uses only the description, service, and component, while routing uses only the subject and description;
- categorization confidence, routing confidence, and similarity score have different meanings and are not interchangeable;
- similar cases are available only after an active dataset is uploaded and are ranked by lexical TF-IDF similarity;
- data and history are local to the current runtime and are not synchronized between application instances;
- only `.xlsx` and `.xls` files up to 25 MB are accepted; the service validates size, extension, signature, XLSX ZIP structure, sheet dimensions, and technical or duplicate headers;
- pickle/joblib files are treated as trusted repository artifacts; an HTTP request cannot select a model path;
- running with `--lifespan off` does not initialize the models.

## Tests

```bash
python -m pytest
```

## Project Structure

```text
app/
  main.py                       FastAPI, lifespan, and HTTP routes
  models.py                     Pydantic API contracts
  services/
    dataset_store.py            Excel import, SQLite, and source records
    preprocessing.py            historical Excel enrichment
    analytics.py                SLA, filters, and historical risk
    categorization.py           categorizer adapter and fallback
    integrations.py             combined module results
    routing.py                  routing and similar-appeal search
  static/                       operator interface
data/
  runtime/                      local databases and uploads, excluded from Git
docs/
  api-contracts.md              API contract documentation
  contracts/                    reference JSON responses
modules/
  categorization/               code, model.pkl, model.json, and report
  routing/                      code, assistant.joblib, metrics, and demo Excel
tests/                          API, frontend, import, security, and integration tests
pyproject.toml                  dependencies and pytest configuration
```

## Development Guidelines

- Do not use the actual support line, resolution, SLA, deadlines, or statuses as features for a new appeal.
- For collaborative development, create separate branches and merge them into `main` through Pull Requests.
- Do not commit virtual environments, caches, SQLite databases, runtime uploads, temporary Excel files, or browser artifacts.
- The bundled `modules/routing/data/raw/Обращения_1931.xlsx` file and test fixtures are the only Excel exceptions.
