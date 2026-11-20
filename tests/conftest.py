import json
import pathlib

import pytest

from ttym.core import SemanticLayer

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def layer() -> SemanticLayer:
    return SemanticLayer()


@pytest.fixture(scope="session")
def answers() -> dict:
    return json.loads((ROOT / "bench" / "answers.json").read_text())
