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
