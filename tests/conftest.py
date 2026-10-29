import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.store import DataStore  # noqa: E402

DATA_DIR = ROOT / "data"


def build_store(tmp_path: Path, extra_schedule=None, fault=None) -> DataStore:
    return DataStore(DATA_DIR, tmp_path / "state.json", extra_schedule=extra_schedule, fault=fault)


@pytest.fixture()
def store(tmp_path):
    return build_store(tmp_path)


@pytest.fixture()
def make_store(tmp_path):
    return lambda **kw: build_store(tmp_path, **kw)
