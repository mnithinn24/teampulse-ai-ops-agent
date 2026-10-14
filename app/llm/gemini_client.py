"""Google Gemini LLM client using official Google GenAI SDK for structured JSON generation."""

import json
import logging
import re
from typing import Any, Optional, Type, TypeVar
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import get_settings
from app.core.errors import AnalysisError, InvalidJSONError
from app.core.retry import is_retryable_llm_error, retry_async

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def clean_json_text(text: str) -> str:
    """Strip markdown code blocks or surrounding whitespace from model text."""
    trimmed = text.strip()
    if trimmed.startswith("```json"):
        trimmed = trimmed[7:]
    elif trimmed.startswith("```"):
        trimmed = trimmed[3:]
    if trimmed.endswith("```"):
        trimmed = trimmed[:-3]
    return trimmed.strip()


class GeminiClient:
    """Asynchronous client for Google Gemini models using the official GenAI SDK."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        thinking_level: Optional[str] = None,
        timeout: Optional[float] = None,
        client: Optional[genai.Client] = None,
    ) -> None:
        """Initialize GeminiClient.

        Args:
            api_key: Gemini API Key (defaults to settings.gemini_api_key).
            model: Gemini model name (defaults to settings.gemini_model). Never hardcoded.
            thinking_level: Optional thinking level (e.g. minimal, low, medium, high).
            timeout: Request timeout in seconds.
            client: Optional pre-configured genai.Client (useful for testing/mocking).
        """
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.thinking_level = thinking_level or settings.gemini_thinking_level
        self.timeout = timeout if timeout is not None else settings.llm_timeout_seconds

        if client is not None:
            self._client = client
        elif self.api_key:
            self._client = genai.Client(api_key=self.api_key)
        else:
            self._client = None

    async def generate_json(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> T:
        """Generate structured JSON adhering to a Pydantic schema using Gemini.

        Args:
            prompt: User prompt content.
            schema: Pydantic model class for output validation.
            system_prompt: Optional system prompt instructions.
            model: Optional model override (e.g. for fallback model).

        Returns:
            Validated instance of the requested Pydantic schema.

        Raises:
            AnalysisError: If Gemini API fails, returns unparseable content, or validation fails.
        """
        settings = get_settings()
        active_model = model or self.model
        if not self._client or not active_model:
            raise AnalysisError(
                message="Gemini client cannot generate content: GEMINI_API_KEY or GEMINI_MODEL is not configured.",
                details={"model": active_model},
            )

        has_fallback = bool(settings.gemini_fallback_model)
        is_main_model = (not model) or (model == (self.model or settings.gemini_model))
        # If fallback model is set, make 1 retry (2 attempts total) on main model for 503 and 429.
        # If no fallback is set, keep 3 attempts (2 retries).
        max_retries = 1 if (has_fallback and is_main_model) else 2

        async def _do_generate() -> T:
            try:
                thinking_config = None
                if self.thinking_level and hasattr(types, "ThinkingConfig"):
                    try:
                        thinking_config = types.ThinkingConfig(thinking_level=self.thinking_level)
                    except Exception as ex:
                        logger.warning("Could not set thinking_level '%s': %s", self.thinking_level, ex)

                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.1,
                    system_instruction=system_prompt if system_prompt else None,
                    thinking_config=thinking_config,
                )

                response = await self._client.aio.models.generate_content(
                    model=active_model,
                    contents=prompt,
                    config=config,
                )

                raw_text = response.text or ""
                cleaned = clean_json_text(raw_text)

                if not cleaned:
                    raise InvalidJSONError(
                        message="Gemini returned empty response text",
                        details={"model": active_model},
                    )

                try:
                    parsed_data = json.loads(cleaned)
                    return schema.model_validate(parsed_data)
                except Exception as parse_exc:
                    raise InvalidJSONError(
                        message=f"Gemini returned invalid JSON for schema {schema.__name__}: {parse_exc}",
                        details={"model": active_model, "raw_text": raw_text, "error": str(parse_exc)},
                    ) from parse_exc

            except Exception as exc:
                logger.error("Gemini generation failed for model %s: %s", active_model, exc)
                if isinstance(exc, AnalysisError):
                    raise
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                raise AnalysisError(
                    message=f"Gemini API failure for model {active_model}: {exc}",
                    details={"model": active_model, "error": str(exc), "code": code},
                ) from exc

        retrier = retry_async(
            max_retries=max_retries,
            base_delay=1.0,
            backoff_factor=2.0,
            retry_if=is_retryable_llm_error,
        )
        return await retrier(_do_generate)()
