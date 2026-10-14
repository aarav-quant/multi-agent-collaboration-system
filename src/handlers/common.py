"""Helpers shared by the agent handlers."""
from __future__ import annotations

from agents.base import AgentResult, run_agent
from common.config import get_config
from common.memory import get_store


def task_prompt_version(task: dict) -> str:
    """Each task records which prompt set it runs with (live or shadow)."""
    return task.get("prompt_version") or get_config().prompt_version


def run_and_record(task_id: str, task: dict, role: str, label: str, index: int,
                   instruction: str, context: str = "") -> AgentResult:
    """Run one agent call and store its token usage + latency on the blackboard."""
    result = run_agent(role, instruction, context, prompt_version=task_prompt_version(task))
    get_store().record_usage(task_id, label, index, result.input_tokens,
                             result.output_tokens, result.latency_ms)
    return result
