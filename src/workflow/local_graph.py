"""The same collaboration workflow as the Step Functions state machine, as a
LangGraph graph, so you can run and debug the whole system on your laptop.
Every node calls the real Lambda handler, so only the routing is duplicated:

  planner --Send x N--> specialist (parallel, round 1)
          --Send x N--> debate     (parallel, peer-to-peer round 2)
          --> writer --> critic --revise--> writer   (cyclic, loop-guarded)
                               --finalize--> END
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from handlers import critic, finalize, planner, specialist, writer


class WorkflowState(TypedDict, total=False):
    task_id: str
    request: str
    subtasks: list[dict]
    specialist_results: Annotated[list[dict], operator.add]  # merged from parallel branches
    debate_results: Annotated[list[dict], operator.add]
    debate_round: int
    revision: int
    feedback: str
    draft_key: str
    approved: bool
    score: Any
    next_action: str
    stop_reason: str
    status: str
    result_key: str


def _fan_out(state: WorkflowState) -> list[Send]:
    # Equivalent of the Step Functions Map state: one branch per subtask
    return [Send("specialist", s) for s in state["subtasks"]]


def _fan_out_debate(state: WorkflowState) -> list[Send]:
    return [Send("debate", {**s, "mode": "debate"}) for s in state["subtasks"]]


def build_workflow():
    g = StateGraph(WorkflowState)
    g.add_node("planner", lambda s: planner.handler(s))
    g.add_node("specialist", lambda s: {"specialist_results": [specialist.handler(s)]})
    g.add_node("prepare_debate", lambda s: {"debate_round": 1})
    g.add_node("debate", lambda s: {"debate_results": [specialist.handler(s)]})
    g.add_node("writer", lambda s: writer.handler(s))
    g.add_node("critic", lambda s: critic.handler(s))
    g.add_node("finalize", lambda s: finalize.handler(s))

    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", _fan_out, ["specialist"])
    g.add_edge("specialist", "prepare_debate")  # runs once all parallel branches finish
    g.add_conditional_edges("prepare_debate", _fan_out_debate, ["debate"])
    g.add_edge("debate", "writer")
    g.add_edge("writer", "critic")
    g.add_conditional_edges("critic",
                            lambda s: "writer" if s["next_action"] == "revise" else "finalize",
                            ["writer", "finalize"])
    g.add_edge("finalize", END)
    return g.compile()
