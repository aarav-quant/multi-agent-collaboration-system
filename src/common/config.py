"""Runtime configuration, read from environment variables.

Lambda gets these from the CDK stack; local runs and tests set them directly.
Read on every call (not cached) so tests can change env vars between cases.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    local_mode: bool            # True -> in-memory DynamoDB/S3/EventBridge stand-ins
    fake_llm: bool              # True -> deterministic fake model, no Bedrock calls
    model_id: str               # Bedrock model or inference-profile id
    table_name: str             # DynamoDB table: shared state + agent memory
    bucket_name: str            # S3 bucket: requests, notes, code, critiques, drafts, results
    event_bus_name: str         # EventBridge bus for lifecycle + peer messages
    state_machine_arn: str      # Step Functions alias ARN (used by intake only)
    max_revisions: int          # loop guard: writer<->critic cycle cap
    max_subtasks: int           # cap on specialists the planner may spawn
    token_budget: int           # cost guard: stop revising once a task used this many tokens
    price_input_per_1k: float   # USD per 1K input tokens (cost estimates only)
    price_output_per_1k: float  # USD per 1K output tokens
    prompt_version: str         # live prompt set (see agents/prompts.py)
    shadow_prompt_version: str  # if set, every task is also shadow-run with this prompt set
    local_output_dir: str       # where local mode writes "S3" objects


def get_config() -> Config:
    env = os.environ.get
    return Config(
        local_mode=env("LOCAL_MODE", "0") == "1",
        fake_llm=env("FAKE_LLM", "0") == "1",
        model_id=env("MODEL_ID", "apac.amazon.nova-lite-v1:0"),
        table_name=env("STATE_TABLE", ""),
        bucket_name=env("ARTIFACT_BUCKET", ""),
        event_bus_name=env("EVENT_BUS_NAME", "default"),
        state_machine_arn=env("STATE_MACHINE_ARN", ""),
        max_revisions=int(env("MAX_REVISIONS", "2")),
        max_subtasks=int(env("MAX_SUBTASKS", "4")),
        token_budget=int(env("TOKEN_BUDGET", "60000")),
        # Defaults approximate Amazon Nova Lite on-demand pricing; check the Bedrock
        # pricing page for your region and override via cdk.json context.
        price_input_per_1k=float(env("PRICE_INPUT_PER_1K", "0.00006")),
        price_output_per_1k=float(env("PRICE_OUTPUT_PER_1K", "0.00024")),
        prompt_version=env("PROMPT_VERSION", "v1"),
        shadow_prompt_version=env("SHADOW_PROMPT_VERSION", ""),
        local_output_dir=env("LOCAL_OUTPUT_DIR", "local_output"),
    )


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    cfg = get_config()
    return round(input_tokens / 1000 * cfg.price_input_per_1k
                 + output_tokens / 1000 * cfg.price_output_per_1k, 6)
