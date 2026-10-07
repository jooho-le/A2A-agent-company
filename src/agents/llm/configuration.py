"""Explicit operator factories. Server bootstrap does not invoke these yet."""

from typing import TYPE_CHECKING

from agents.llm.contracts import LLMErrorCode, LLMProvider, LLMRuntimeError
from orchestrator.domain.run_configuration import ModelConfiguration

if TYPE_CHECKING:
    from agents.core.config import AgentSettings


def model_from_settings(
    settings: "AgentSettings", *, frozen_model: ModelConfiguration | None = None,
) -> ModelConfiguration:
    """No invented selection/temperature and no switch from a frozen Run model."""
    if settings.llm_provider is None or settings.llm_model_id is None or settings.llm_temperature is None:
        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
    try:
        selected = ModelConfiguration(
            provider=settings.llm_provider, model_id=settings.llm_model_id,
            model_revision=settings.llm_model_revision,
            temperature=settings.llm_temperature, seed=settings.llm_seed,
        )
    except Exception:
        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None
    if frozen_model is not None and selected != frozen_model:
        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
    return selected


def provider_from_settings(settings: "AgentSettings") -> LLMProvider:
    """Currently only the optional OpenAI adapter; others must be implemented."""
    model = model_from_settings(settings)
    if (
        settings.llm_provider != "openai" or settings.llm_api_key is None
        or model.seed is not None or model.temperature > 2
    ):
        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
    from agents.llm.openai_responses import OpenAIResponsesProvider
    return OpenAIResponsesProvider(api_key=settings.llm_api_key)
