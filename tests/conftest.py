import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="ap_test_")
os.environ["AP_DB_PATH"] = str(Path(_tmp) / "test.db")   # never touch the demo database
os.environ.pop("GEMINI_API_KEY", None)                     # tests are offline by default

import json  # noqa: E402

import pytest  # noqa: E402

INV = ROOT / "data" / "invoices"


def pytest_configure(config):
    config.addinivalue_line("markers", "live: opt-in live LLM smoke test (needs GEMINI_API_KEY)")


@pytest.fixture
def fresh_db():
    from src import storage
    storage.init_db(reset=True)
    yield storage


def load(*names):
    return [(n, (INV / n).read_bytes()) for n in names]


@pytest.fixture
def scenarios():
    return {s["id"]: s for s in json.loads((ROOT / "data" / "sample_scenarios.json").read_text(encoding="utf-8"))}


@pytest.fixture
def expected():
    return json.loads((ROOT / "data" / "expected_scenarios.json").read_text(encoding="utf-8"))
