"""One agent = one small LangGraph graph.

    START -> reason -> parse --(valid)--> END
                 ^        |
                 +--------+ (invalid JSON and attempts left: retry with the error)

Step Functions coordinates agents with each other; LangGraph handles the
reasoning loop inside each agent (call model, validate output, self-correct).
Every call reports token usage and latency for cost tracking.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.prompts import JSON_ROLES, render_user_prompt, system_prompt
from common.llm import get_llm

MAX_ATTEMPTS = 2

# Bedrock error codes worth retrying; Step Functions retries on RetryableAgentError
_RETRYABLE_CODES = {"ThrottlingException", "ServiceUnavailableException",
                    "ModelNotReadyException", "ModelTimeoutException",
                    "InternalServerException"}


class RetryableAgentError(Exception):
    """Transient model failure. The workflow retries with backoff."""


class AgentOutputError(Exception):
    """The model never produced output in the required format."""


@dataclass
class AgentResult:
    output: Any
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int


class AgentState(TypedDict, total=False):
    task: str
    context: str
    raw: str          # last raw model reply
    output: Any       # parsed result (str, or dict for JSON roles)
    attempts: int
    error: str        # parse error from the last attempt ("" if none)
    input_tokens: int
    output_tokens: int


def _text(resp) -> str:
    content = resp.content
    if isinstance(content, str):
        return content
    # Converse can return a list of content blocks
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


def _usage(resp) -> tuple[int, int]:
    meta = getattr(resp, "usage_metadata", None) or {}
    return int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))


def _is_retryable(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)  # botocore ClientError carries the code here
    code = resp.get("Error", {}).get("Code") if isinstance(resp, dict) else None
    return code in _RETRYABLE_CODES or type(exc).__name__ in _RETRYABLE_CODES


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e.msg}") from e


def build_agent_graph(role: str, llm, prompt_version: str = "v1"):
    sys_prompt = system_prompt(role, prompt_version)

    def reason(state: AgentState) -> dict:
        msgs = [SystemMessage(content=sys_prompt),
                HumanMessage(content=render_user_prompt(state["task"], state.get("context", "")))]
        if state.get("error"):  # self-correction turn
            msgs += [AIMessage(content=state["raw"]),
                     HumanMessage(content=f"That reply could not be parsed ({state['error']}). "
                                          "Reply again with only the JSON object.")]
        try:
            resp = llm.invoke(msgs)
        except Exception as e:
            if _is_retryable(e):
                raise RetryableAgentError(str(e)) from e
            raise
        tin, tout = _usage(resp)
        return {"raw": _text(resp), "attempts": state.get("attempts", 0) + 1,
                "input_tokens": state.get("input_tokens", 0) + tin,
                "output_tokens": state.get("output_tokens", 0) + tout}

    def parse(state: AgentState) -> dict:
        if role not in JSON_ROLES:
            return {"output": state["raw"].strip(), "error": ""}
        try:
            return {"output": extract_json(state["raw"]), "error": ""}
        except ValueError as e:
            return {"error": str(e)}

    def route(state: AgentState) -> str:
        if state.get("error") and state["attempts"] < MAX_ATTEMPTS:
            return "reason"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("reason", reason)
    graph.add_node("parse", parse)
    graph.add_edge(START, "reason")
    graph.add_edge("reason", "parse")
    graph.add_conditional_edges("parse", route, ["reason", END])
    return graph.compile()


def run_agent(role: str, task: str, context: str = "", prompt_version: str = "v1") -> AgentResult:
    started = time.perf_counter()
    final = build_agent_graph(role, get_llm(), prompt_version).invoke(
        {"task": task, "context": context, "attempts": 0})
    if final.get("error"):
        raise AgentOutputError(
            f"{role} gave unparseable output after {final['attempts']} attempts: {final['error']}")
    return AgentResult(output=final["output"],
                       input_tokens=final.get("input_tokens", 0),
                       output_tokens=final.get("output_tokens", 0),
                       latency_ms=int((time.perf_counter() - started) * 1000),
                       attempts=final["attempts"])
