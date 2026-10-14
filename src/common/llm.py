"""Chat model factory: Amazon Bedrock (Converse API) or a fake for offline runs."""
from __future__ import annotations

from common.config import get_config

_llm = None


def get_llm():
    global _llm
    if _llm is None:
        cfg = get_config()
        if cfg.fake_llm:
            from common.fake_llm import FakeLLM
            _llm = FakeLLM()
        else:
            from langchain_aws import ChatBedrockConverse
            _llm = ChatBedrockConverse(model=cfg.model_id, temperature=0.2, max_tokens=4096)
    return _llm


def set_llm(llm) -> None:
    """Inject a model (tests use this to script specific replies)."""
    global _llm
    _llm = llm


def reset() -> None:
    set_llm(None)
