import json
import pathlib
import sys

import pytest

from fakes import FakeLLM, call, final

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bench"))
import run as runner  # noqa: E402
from score import copy_differs  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def factory(script):
    def make():
        llm = FakeLLM(list(script))
        llm.model = "fake"
        return llm
    return make


async def test_setup_c_saves_runs_and_skips_unchanged(tmp_path, answers):
    script = [call("query_metrics", {"metrics": ["delivered_orders"], "start": "2018-03-01", "end": "2018-03-31"}),
              final({"answer": "x", "columns": ["delivered_orders"], "rows": [[7003]]})]
    first = await runner.run("C", "dev", ["d02"], tmp_path, llm_factory=factory(script))
    assert first["done"] == 1
    saved = json.loads((tmp_path / "runs-dev" / "C" / "d02.json").read_text())
    assert saved["final"]["rows"] == [[answers["d02"]["rows"][0][0]]] and saved["fingerprint"]
    assert copy_differs(saved) is False
    second = await runner.run("C", "dev", ["d02"], tmp_path, llm_factory=factory(script))
    assert second == {**second, "done": 0, "skipped": 1}


async def test_setup_a_runs_sql_on_raw_tables(tmp_path):
    sql = "select count(*) as n from raw.orders where order_status = 'delivered' and order_purchase_timestamp >= '2018-03-01' and order_purchase_timestamp < '2018-04-01'"
    script = [call("get_schema", {}), call("run_sql", {"sql": sql}, id="c2"),
              final({"answer": "x", "columns": ["n"], "rows": [[7003]]})]
    result = await runner.run("A", "dev", ["d02"], tmp_path, llm_factory=factory(script))
    saved = json.loads((tmp_path / "runs-dev" / "A" / "d02.json").read_text())
    assert result["done"] == 1 and saved["trace"][1]["result"]["rows"] == [[7003]]
    assert "raw.orders" in saved["trace"][0]["result"]["schema"]


async def test_setup_a_cannot_reach_semantic_tables(tmp_path):
    script = [call("run_sql", {"sql": "select count(*) from main.fct_orders"}), final({"cannot_answer": "x"})]
    await runner.run("A", "dev", ["du1"], tmp_path, llm_factory=factory(script))
    saved = json.loads((tmp_path / "runs-dev" / "A" / "du1.json").read_text())
    assert "error" in saved["trace"][0]["result"]


async def test_quota_stops_the_run(tmp_path):
    from ttym.llm import QuotaExhausted

    class Exhausted(FakeLLM):
        async def chat(self, *a, **k):
            raise QuotaExhausted("used up")

    def make():
        llm = Exhausted([])
        llm.model = "fake"
        return llm

    result = await runner.run("C", "dev", ["d01", "d02"], tmp_path, llm_factory=make)
    assert result["stopped"] == "d01" and result["done"] == 0
    assert not list((tmp_path / "runs-dev" / "C").glob("*.json"))


async def test_provider_error_is_saved_as_a_failed_run_and_logged(tmp_path):
    from ttym.llm import LLMError

    class Flaky(FakeLLM):
        async def chat(self, *a, **k):
            if not self.script:
                raise LLMError("the model returned 500: upstream")
            return await super().chat(*a, **k)

    def make():
        llm = Flaky([call("list_metrics", {})])
        llm.model = "fake"
        return llm

    result = await runner.run("C", "dev", ["d01", "d02"], tmp_path, llm_factory=make)
    assert result["done"] == 2 and result["stopped"] is None
    saved = json.loads((tmp_path / "runs-dev" / "C" / "d01.json").read_text())
    assert saved["final"] is None and saved["error"].startswith("provider error: the model returned 500")
    assert saved["trace"][0]["tool"] == "list_metrics"
    log = (tmp_path / "logs" / "dev-C.log").read_text()
    assert "d01" in log and "failed: provider error" in log and "d02" in log
    again = await runner.run("C", "dev", ["d01", "d02"], tmp_path, llm_factory=make)
    assert again["done"] == 0 and again["skipped"] == 2


def test_setup_a_plus_adds_only_the_definitions():
    _, a = runner.build("A")
    _, a_plus = runner.build("A+")
    assert a_plus.startswith(a) and "customer_unique_id" in a_plus and "Excludes freight" in a_plus
    assert "questions.yaml" not in a_plus and "semantic model" not in a_plus
