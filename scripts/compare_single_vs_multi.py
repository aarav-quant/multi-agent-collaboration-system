"""Cost / latency / quality trade-off: single agent vs the multi-agent system.

Runs the same request two ways on the same Bedrock model:
  1. single agent  - one well-prompted call (no planning, debate or review)
  2. multi-agent   - the deployed workflow (planner, specialists, debate, writer, critic)
Then an independent judge call scores both answers 1-10 with the same rubric.

  python scripts/compare_single_vs_multi.py "Design a URL shortener and pick a database"
  python scripts/compare_single_vs_multi.py --task-id <existing multi-agent task> "same request"

Uses boto3 only (no LangChain), so it runs anywhere with AWS credentials, e.g. CloudShell.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mas import STACK, outputs, read_s3, submit, wait  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SINGLE_PROMPT = ("You are a senior expert. Answer the request thoroughly in Markdown: a short "
                 "summary, detailed analysis of options with trade-offs, code where useful, a "
                 "clear recommendation and next steps.")
JUDGE_PROMPT = ("You are an impartial judge. Score the answer to the request from 1 to 10 for "
                "completeness, correctness, clarity and soundness of the recommendation. "
                'Reply with ONLY JSON: {"score": <1-10>, "reason": "<one sentence>"}')


def converse(client, model_id: str, system: str, text: str, max_tokens: int = 4096):
    started = time.perf_counter()
    resp = client.converse(modelId=model_id, system=[{"text": system}],
                           messages=[{"role": "user", "content": [{"text": text}]}],
                           inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2})
    latency = time.perf_counter() - started
    answer = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    return answer, resp["usage"]["inputTokens"], resp["usage"]["outputTokens"], latency


def judge(client, model_id: str, request: str, answer: str) -> int:
    text, *_ = converse(client, model_id, JUDGE_PROMPT,
                        f"REQUEST:\n{request}\n\nANSWER:\n{answer}", max_tokens=200)
    try:
        return int(json.loads(text[text.find("{"):text.rfind("}") + 1])["score"])
    except (ValueError, KeyError):
        return 0


def main() -> None:
    ctx = json.loads((ROOT / "cdk.json").read_text())["context"]
    p = argparse.ArgumentParser()
    p.add_argument("request")
    p.add_argument("--task-id", help="reuse a finished multi-agent task for this request")
    p.add_argument("--model-id", default=ctx["model_id"])
    p.add_argument("--stack", default=STACK)
    args = p.parse_args()
    price_in, price_out = float(ctx["price_input_per_1k"]), float(ctx["price_output_per_1k"])
    out = outputs(args.stack)
    bedrock = boto3.client("bedrock-runtime")

    print("running multi-agent workflow ...", file=sys.stderr)
    multi = wait(out, args.task_id or submit(out, args.request))
    multi_answer = read_s3(out, multi["result_key"])

    print("running single agent ...", file=sys.stderr)
    single_answer, tin, tout, latency = converse(bedrock, args.model_id, SINGLE_PROMPT, args.request)

    print("judging both answers ...", file=sys.stderr)
    rows = {
        "single agent": (tin + tout, tin / 1000 * price_in + tout / 1000 * price_out, latency,
                         1, judge(bedrock, args.model_id, args.request, single_answer)),
        "multi-agent": (int(multi["total_tokens"]), float(multi["est_cost_usd"]),
                        float(multi["wall_clock_s"]), int(multi["agent_calls"]),
                        judge(bedrock, args.model_id, args.request, multi_answer)),
    }
    print(f"\n{'':14}{'tokens':>10}{'cost $':>12}{'latency s':>11}{'LLM calls':>11}{'score':>7}")
    for name, (tok, cost, lat, calls, score) in rows.items():
        print(f"{name:14}{tok:>10}{cost:>12.5f}{lat:>11.1f}{calls:>11}{score:>7}")
    s, m = rows["single agent"], rows["multi-agent"]
    print(f"\nMulti-agent costs {m[1] / max(s[1], 1e-9):.1f}x and takes {m[2] / max(s[2], 1e-9):.1f}x "
          f"as long, for a score change of {m[4] - s[4]:+d}.")


if __name__ == "__main__":
    main()
