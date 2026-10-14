"""Static termination checks on the versioned Step Functions definition (asl/).

A cyclic agent graph must not be able to loop forever. These tests parse the
ASL that is tracked in git and prove that:
  * every cycle passes through a Choice state that guards it with a numeric
    upper bound on the revision counter (loop guard),
  * the execution has a hard timeout,
  * every path can reach a terminal state (Succeed / Fail),
  * Map iterators (parallel agents) contain no cycles.
"""
import json
from pathlib import Path

ASL = json.loads((Path(__file__).resolve().parents[1] / "asl" /
                  "collaboration_workflow.asl.json").read_text())


def edges(states: dict) -> dict[str, set[str]]:
    out = {}
    for name, st in states.items():
        nxt = {st[k] for k in ("Next", "Default") if k in st}
        nxt |= {c["Next"] for c in st.get("Choices", [])}
        nxt |= {c["Next"] for c in st.get("Catch", [])}
        out[name] = nxt
    return out


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """All simple cycles (small graph, DFS is fine)."""
    found = []

    def dfs(start, node, path, seen):
        for nxt in graph.get(node, ()):
            if nxt == start:
                found.append(path[:])
            elif nxt not in seen and nxt > start:   # canonical start = smallest name
                dfs(start, nxt, path + [nxt], seen | {nxt})

    for s in graph:
        dfs(s, s, [s], {s})
    return found


def has_numeric_guard(condition: dict) -> bool:
    if any(k.startswith("Numeric") and k.endswith(("LessThan", "LessThanEquals"))
           for k in condition) and condition.get("Variable") == "$.revision":
        return True
    return any(has_numeric_guard(c) for c in condition.get("And", []))


def guarded(state: dict) -> bool:
    return state["Type"] == "Choice" and all(has_numeric_guard(c) for c in state["Choices"])


def test_every_cycle_has_a_loop_guard():
    states = ASL["States"]
    found = cycles(edges(states))
    assert found, "expected the writer<->critic revision cycle"
    for cyc in found:
        assert any(guarded(states[s]) for s in cyc), f"unguarded cycle: {' -> '.join(cyc)}"


def test_execution_has_hard_timeout():
    assert 0 < ASL.get("TimeoutSeconds", 0) <= 3600


def test_every_state_can_reach_an_end():
    states, graph = ASL["States"], edges(ASL["States"])
    terminal = {n for n, s in states.items() if s["Type"] in ("Succeed", "Fail") or s.get("End")}
    for name in states:
        seen, stack = set(), [name]
        while stack:
            cur = stack.pop()
            if cur in terminal:
                break
            if cur not in seen:
                seen.add(cur)
                stack.extend(graph[cur])
        else:
            raise AssertionError(f"{name} cannot reach a terminal state")


def test_parallel_agent_iterators_are_acyclic():
    maps = [s for s in ASL["States"].values() if s["Type"] == "Map"]
    assert len(maps) == 2  # specialists + peer debate
    for m in maps:
        assert not cycles(edges(m["ItemProcessor"]["States"]))
