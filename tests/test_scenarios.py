"""CI scenario suite: fixed task scenarios run through the full agent graph.

For every scenario in tests/scenarios/*.json this checks:
  * termination  - the cyclic graph ends (a LangGraph recursion limit acts as an
                   infinite-loop detector) with the expected stop reason
  * behaviour    - revisions / number of agent calls match expectations
  * cost         - COST GUARDRAIL: total tokens and estimated USD stay within
                   the scenario's budget, otherwise the build fails
"""
import json
from pathlib import Path

import pytest
from langgraph.errors import GraphRecursionError

from common import llm
from common.fake_llm import FakeLLM
from common.memory import get_store
from handlers import intake
from workflow.local_graph import build_workflow

SCENARIOS = sorted(Path(__file__).parent.glob("scenarios/*.json"))
RECURSION_LIMIT = 40  # far above any legitimate run; hitting it means a runaway loop


def load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
def test_scenario(path, monkeypatch):
    sc = load(path)
    for key, value in sc["env"].items():
        monkeypatch.setenv(key, value)
    llm.set_llm(FakeLLM(sc["fake_script"]))

    task_id = intake.create_task(sc["request"])
    try:
        build_workflow().invoke({"task_id": task_id}, {"recursion_limit": RECURSION_LIMIT})
    except GraphRecursionError:
        pytest.fail(f"{sc['name']}: agent graph did not terminate (infinite loop)")

    task = get_store().get_task(task_id)
    assert task["status"] == "COMPLETED", f"{sc['name']}: ended as {task['status']}"
    for key, expected in sc["expect"].items():
        assert task[key] == expected, f"{sc['name']}: {key}={task[key]!r}, expected {expected!r}"

    budget = sc["budget"]
    assert task["total_tokens"] <= budget["max_total_tokens"], (
        f"COST GUARDRAIL: {sc['name']} used {task['total_tokens']} tokens "
        f"(budget {budget['max_total_tokens']})")
    assert task["est_cost_usd"] <= budget["max_cost_usd"], (
        f"COST GUARDRAIL: {sc['name']} cost ${task['est_cost_usd']} (budget ${budget['max_cost_usd']})")


def test_every_scenario_is_well_formed():
    assert SCENARIOS, "no scenarios found"
    for path in SCENARIOS:
        sc = load(path)
        assert {"name", "request", "fake_script", "env", "expect", "budget", "live",
                "live_budget"} <= sc.keys(), path.name
        assert sc["name"] == path.stem
