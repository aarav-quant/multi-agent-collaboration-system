"""Deterministic stand-in for Bedrock so the whole system runs offline (tests, CI).

It reads the "ROLE: <name>" line from the system prompt and answers in the
format that role must produce. The critic rejects the first draft and
approves the second, which exercises the revision loop. Token usage is
estimated as characters / 4 so cost-guardrail tests have real numbers.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from langchain_core.messages import AIMessage


class FakeLLM:
    def __init__(self, script: dict[str, list[str]] | None = None):
        self.script = script or {}           # role -> replies, returned in order
        self.calls: dict[str, int] = defaultdict(int)

    def invoke(self, messages):
        role = re.search(r"ROLE:\s*(\w+)", messages[0].content).group(1)
        is_debate = "DEBATE ROUND" in messages[1].content
        key = f"{role}-debate" if is_debate else role
        self.calls[key] += 1
        n = self.calls[key]
        if key in self.script:
            replies = self.script[key]
            reply = replies[min(n, len(replies)) - 1]
        else:
            reply = self._default(role, n, is_debate)
        prompt_chars = sum(len(m.content) for m in messages)
        tin, tout = prompt_chars // 4, len(reply) // 4
        return AIMessage(content=reply, usage_metadata={
            "input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout})

    @staticmethod
    def _default(role: str, n: int, is_debate: bool) -> str:
        if is_debate:
            return (f"AGREE: most points (fake {role}).\n"
                    f"DISAGREE: cost estimate looks optimistic (fake {role}).\n"
                    f"REVISED POSITION: keep option A, add a cost buffer.")
        if role == "planner":
            return json.dumps({"subtasks": [
                {"agent": "researcher", "instruction": "Collect the key facts and background."},
                {"agent": "coder", "instruction": "Sketch the core API in Python."},
                {"agent": "analyst", "instruction": "Weigh trade-offs and give a recommendation."},
            ]})
        if role == "researcher":
            return "- Fact 1 (fake researcher)\n- Fact 2 (fake researcher)"
        if role == "coder":
            return "```python\ndef handler(event):\n    return {'ok': True}\n```\nSimple handler (fake coder)."
        if role == "analyst":
            return "Recommendation: option A, because it is cheaper (fake analyst)."
        if role == "writer":
            return f"# Report (draft {n})\n\nCombined findings from the specialists."
        if role == "critic":
            approved = n >= 2
            return json.dumps({"approved": approved, "score": 8 if approved else 5,
                               "unresolved_conflicts": [] if approved else ["cost estimate"],
                               "feedback": "" if approved else "Resolve the cost disagreement."})
        return "ok"
