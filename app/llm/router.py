"""LLM router for selecting models based on task complexity, health, and fallback policies."""

from dataclasses import dataclass
import logging
import time
from typing import Any, Dict, Generic, Optional, Type, TypeVar
from pydantic import BaseModel

from app.config import get_settings
from app.core.errors import AnalysisError
from app.llm.gemini_client import GeminiClient
from app.llm.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

SIMPLE_TASK_TYPES = {"intent", "classification", "query_intent", "simple"}


@dataclass
class LLMResult(Generic[T]):
    """Structured response from LLM router with execution metadata."""

    data: T
    model_name: str
    provider: str  # "ollama" | "gemini"
    task_type: str
    latency_ms: float


class LLMRouter:
    """Intelligent router directing requests to Ollama or Gemini with resilience and fallbacks."""

    def __init__(
        self,
        ollama_client: Optional[OllamaClient] = None,
        gemini_client: Optional[GeminiClient] = None,
    ) -> None:
        """Initialize the router.

        Args:
            ollama_client: Optional pre-configured Ollama client.
            gemini_client: Optional pre-configured Gemini client.
        """
        self.ollama_client = ollama_client or OllamaClient()
        self.gemini_client = gemini_client or GeminiClient()

    async def route(
        self,
        prompt: str,
        schema: Type[T],
        task_type: str = "analysis",
        system_prompt: Optional[str] = None,
    ) -> LLMResult[T]:
        """Route request to the optimal model based on task type and availability.

        Policy:
        - Simple tasks (intent, classification) -> Local/LAN Ollama
        - Complex tasks (analysis, long-context) -> Cloud Gemini
        - If Ollama is disabled or unreachable -> Gemini only
        - If preferred model fails -> Automatic fallback to alternate model
        - Latency, model name, and task type logged on every call

        Args:
            prompt: User prompt content.
            schema: Expected Pydantic output schema.
            task_type: Identifier of task nature ('intent', 'analysis', etc.).
            system_prompt: Optional system prompt instructions.

        Returns:
            LLMResult containing the validated schema instance and metadata.

        Raises:
            AnalysisError: If both preferred and fallback models fail.
        """
        settings = get_settings()
        is_simple_task = task_type.lower() in SIMPLE_TASK_TYPES

        # Check Ollama feasibility
        ollama_available = False
        if settings.ollama_enabled:
            ollama_available = await self.ollama_client.check_health()

        # Build prioritized list of (provider, model_name) to attempt
        candidates: List[Tuple[str, str]] = []
        if is_simple_task and ollama_available:
            candidates.append(("ollama", settings.ollama_model))
            if settings.gemini_api_key and settings.gemini_model:
                candidates.append(("gemini", settings.gemini_model))
            if settings.gemini_api_key and settings.gemini_fallback_model:
                candidates.append(("gemini_fallback", settings.gemini_fallback_model))
        else:
            if settings.gemini_api_key and settings.gemini_model:
                candidates.append(("gemini", settings.gemini_model))
            if settings.gemini_api_key and settings.gemini_fallback_model:
                candidates.append(("gemini_fallback", settings.gemini_fallback_model))
            if ollama_available:
                candidates.append(("ollama", settings.ollama_model))

        if not candidates:
            raise AnalysisError("No configured LLM providers available (check GEMINI_API_KEY or OLLAMA_BASE_URL).")

        errors: List[str] = []
        for provider, model_name in candidates:
            try:
                logger.info("Invoking %s (model: %s) for task '%s'", provider, model_name, task_type)
                return await self._call_provider(
                    provider=provider,
                    prompt=prompt,
                    schema=schema,
                    task_type=task_type,
                    system_prompt=system_prompt,
                    model_override=model_name,
                )
            except Exception as exc:
                logger.warning(
                    "Model candidate %s (%s) failed for task '%s': %s",
                    provider,
                    model_name,
                    task_type,
                    exc,
                )
                errors.append(f"{provider}({model_name}): {exc}")

        raise AnalysisError(
            message=f"All attempted LLM models failed for task '{task_type}': {'; '.join(errors)}",
            details={"task_type": task_type, "errors": errors},
        )

    async def _call_provider(
        self,
        provider: str,
        prompt: str,
        schema: Type[T],
        task_type: str,
        system_prompt: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResult[T]:
        """Execute call against a specific provider and measure latency in milliseconds."""
        settings = get_settings()
        start = time.perf_counter()

        if provider == "ollama":
            model_name = model_override or settings.ollama_model
            data = await self.ollama_client.generate_json(
                prompt=prompt,
                schema=schema,
                system_prompt=system_prompt,
            )
        elif provider in ("gemini", "gemini_fallback"):
            model_name = model_override or (settings.gemini_model if provider == "gemini" else settings.gemini_fallback_model)
            data = await self.gemini_client.generate_json(
                prompt=prompt,
                schema=schema,
                system_prompt=system_prompt,
                model=model_name,
            )
        else:
            raise AnalysisError(f"Unknown provider '{provider}'")

        latency_ms = (time.perf_counter() - start) * 1000.0
        logger.info(
            "LLM call succeeded: provider=%s model=%s task_type=%s latency_ms=%.2f",
            provider,
            model_name,
            task_type,
            latency_ms,
        )

        return LLMResult(
            data=data,
            model_name=model_name,
            provider=provider,
            task_type=task_type,
            latency_ms=round(latency_ms, 2),
        )
