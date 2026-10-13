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

        # Decide preferred execution order
        gemini_configured = bool(settings.gemini_api_key and settings.gemini_model)
        if is_simple_task and ollama_available:
            primary_provider = "ollama"
            fallback_provider = "gemini" if gemini_configured else None
        else:
            primary_provider = "gemini" if gemini_configured else "ollama"
            fallback_provider = "ollama" if (ollama_available and primary_provider == "gemini") else None

        # Attempt primary provider
        try:
            return await self._call_provider(
                provider=primary_provider,
                prompt=prompt,
                schema=schema,
                task_type=task_type,
                system_prompt=system_prompt,
            )
        except Exception as primary_err:
            logger.warning(
                "Primary model (%s) failed for task '%s': %s",
                primary_provider,
                task_type,
                primary_err,
            )

            # Attempt fallback if available
            if fallback_provider and fallback_provider != primary_provider:
                logger.info(
                    "Attempting fallback to %s for task '%s'",
                    fallback_provider,
                    task_type,
                )
                try:
                    return await self._call_provider(
                        provider=fallback_provider,
                        prompt=prompt,
                        schema=schema,
                        task_type=task_type,
                        system_prompt=system_prompt,
                    )
                except Exception as fallback_err:
                    logger.error(
                        "Fallback model (%s) also failed: %s",
                        fallback_provider,
                        fallback_err,
                    )
                    raise AnalysisError(
                        message=f"Both primary ({primary_provider}) and fallback ({fallback_provider}) models failed: {fallback_err}",
                        details={
                            "primary_error": str(primary_err),
                            "fallback_error": str(fallback_err),
                            "task_type": task_type,
                        },
                    ) from fallback_err

            raise AnalysisError(
                message=f"Model execution failed for {primary_provider} and no fallback available: {primary_err}",
                details={"primary_error": str(primary_err), "task_type": task_type},
            ) from primary_err

    async def _call_provider(
        self,
        provider: str,
        prompt: str,
        schema: Type[T],
        task_type: str,
        system_prompt: Optional[str] = None,
    ) -> LLMResult[T]:
        """Execute call against a specific provider and measure latency in milliseconds."""
        settings = get_settings()
        start = time.perf_counter()

        if provider == "ollama":
            model_name = settings.ollama_model
            data = await self.ollama_client.generate_json(
                prompt=prompt,
                schema=schema,
                system_prompt=system_prompt,
            )
        elif provider == "gemini":
            model_name = settings.gemini_model
            data = await self.gemini_client.generate_json(
                prompt=prompt,
                schema=schema,
                system_prompt=system_prompt,
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
