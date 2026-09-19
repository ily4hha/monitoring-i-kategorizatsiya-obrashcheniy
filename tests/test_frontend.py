from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_regressions_with_real_handlers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute frontend regressions")
    result = subprocess.run([node, "--test", "tests/frontend.test.cjs"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_file_input_focus_selector_targets_its_visible_label():
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))
    parser = Tags()
    parser.feed((ROOT / "app/static/index.html").read_text())
    index = next(i for i, (_, attrs) in enumerate(parser.tags) if attrs.get("id") == "dataset-file")
    tag, attributes = parser.tags[index]
    assert tag == "input" and attributes["type"] == "file"
    assert attributes.get("tabindex") != "-1" and attributes.get("aria-label")
    next_tag, label = parser.tags[index + 1]
    assert next_tag == "label" and label.get("for") == "dataset-file" and label.get("class") == "upload-button"
    css = (ROOT / "app/static/styles.css").read_text()
    assert '#dataset-file:focus-visible + .upload-button { outline: 3px solid white; outline-offset: 3px; }' in css


def test_integrated_result_and_sla_elements_are_present():
    html = (ROOT / "app/static/index.html").read_text()
    for element_id in (
        "classification-status", "category-confidence-label", "category-limitation",
        "category-review", "routing-status", "line-confidence-label", "line-review",
        "similarity-status", "kpi-mean-sla",
        "kpi-median-sla", "kpi-multiline", "kpi-clarifications", "analytics-message",
        "category-distribution", "line-distribution", "sla-services", "sla-categories",
        "sla-priorities", "sla-lines", "risk-categories", "risk-lines",
    ):
        assert f'id="{element_id}"' in html
    assert 'name="component"' in html


def test_three_sections_filters_and_human_language_are_present():
    html = (ROOT / "app/static/index.html").read_text()
    script = (ROOT / "app/static/app.js").read_text()
    for view in ("operator-view", "history-view", "analytics-view"):
        assert f'id="{view}"' in html
    assert 'id="operator-view" class="view active"' in html
    assert '<button class="primary" type="submit">Проанализировать</button>' in html
    assert '<link rel="icon" href="data:image/svg+xml,' in html
    for field in ("date_from", "date_to", "service", "category", "priority", "line"):
        assert f'name="{field}"' in html
    assert "Открыть исходную запись" in script
    assert "не является прогнозом" in html
    visible_copy = html.lower()
    for internal_term in ("final.csv", "partial-data", "model-incompatible", "fact_sla_h", "is_overdue"):
        assert internal_term not in visible_copy
