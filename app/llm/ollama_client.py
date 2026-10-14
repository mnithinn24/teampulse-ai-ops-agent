"""Ollama LLM client for local/LAN inference with structured JSON output and think-tag stripping."""

import json
import logging
import re
from typing import Any, Dict, Optional, Type, TypeVar
import httpx
from pydantic import BaseModel

from app.config import get_settings
from app.core.errors import AnalysisError, InvalidJSONError
from app.core.retry import is_retryable_llm_error, retry_async

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

THINK_REGEX = re.compile(r"<think>.*?</think>", flags=re.DOTALL)


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks emitted by thinking models.

    Args:
        text: Raw model output string.

    Returns:
        Cleaned string with all thinking tags and enclosed text removed.
    """
    return THINK_REGEX.sub("", text).strip()


class OllamaClient:
    """Asynchronous client for interacting with an Ollama instance."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Initialize OllamaClient.

        Args:
            base_url: Ollama host base URL (defaults to settings.ollama_base_url).
            model: Model name to use (defaults to settings.ollama_model). Never hardcoded.
            timeout: Request timeout in seconds (defaults to settings.llm_timeout_seconds).
            client: Optional pre-configured httpx.AsyncClient instance.
        """
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout if timeout is not None else settings.llm_timeout_seconds

        self._external_client = client is not None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=self.timeout)

    async def check_health(self) -> bool:
        """Check if the Ollama server is reachable and responsive.

        Returns:
            True if Ollama is available, False otherwise.
        """
        url = f"{self.base_url}/api/tags"
        try:
            response = await self._client.get(url, timeout=5.0)
            return response.status_code == 200
        except Exception as exc:
            logger.debug("Ollama health check failed for %s: %s", url, exc)
            return False

    @retry_async(
        max_retries=3,
        base_delay=2.0,
        backoff_factor=2.25,
        retry_if=is_retryable_llm_error,
        retryable_exceptions=(httpx.RequestError, httpx.HTTPStatusError, AnalysisError),
    )
    async def _post_chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send chat request to Ollama with retry.

        Args:
            payload: JSON dictionary for POST /api/chat.

        Returns:
            Parsed JSON response from Ollama.

        Raises:
            AnalysisError: On HTTP error or network failure.
        """
        url = f"{self.base_url}/api/chat"
        try:
            response = await self._client.post(url, json=payload, timeout=self.timeout)
            if response.status_code in (429, 503):
                response.raise_for_status()
            if response.is_error:
                raise AnalysisError(
                    message=f"Ollama API error {response.status_code}: {response.text}",
                    details={"url": url, "status_code": response.status_code},
                )
            return response.json()
        except httpx.HTTPStatusError as exc:
            if not is_retryable_llm_error(exc):
                raise AnalysisError(
                    message=f"Ollama API error {exc.response.status_code}: {exc}",
                    details={"url": url, "status_code": exc.response.status_code},
                ) from exc
            raise
        except httpx.RequestError as exc:
            logger.error("Network error communicating with Ollama at %s: %s", url, exc)
            raise AnalysisError(
                message=f"Could not connect to Ollama at {self.base_url}: {exc}",
                details={"url": url, "error": str(exc)},
            ) from exc

    async def generate_json(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: Optional[str] = None,
    ) -> T:
        """Generate structured JSON conforming to a Pydantic schema using Ollama.

        Args:
            prompt: User prompt content.
            schema: Pydantic model class defining desired output structure.
            system_prompt: Optional system prompt instructions.

        Returns:
            Validated instance of the requested Pydantic schema.

        Raises:
            AnalysisError: If Ollama fails, returns malformed JSON, or schema validation fails.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Provide Pydantic JSON schema in Ollama format parameter
        json_schema = schema.model_json_schema()

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": json_schema,
            "options": {
                "temperature": 0.1,
            },
        }

        data = await self._post_chat(payload)
        message = data.get("message", {})
        raw_content = message.get("content", "")

        # Strip any <think>...</think> blocks from models with reasoning traces
        cleaned_content = strip_think_tags(raw_content)

        if not cleaned_content:
            raise InvalidJSONError(
                message="Ollama returned empty response content",
                details={"raw_response": data},
            )

        try:
            parsed_json = json.loads(cleaned_content)
            return schema.model_validate(parsed_json)
        except Exception as exc:
            logger.warning("Ollama response failed schema validation: %s. Content: %s", exc, cleaned_content)
            raise InvalidJSONError(
                message=f"Ollama output failed validation for schema {schema.__name__}: {exc}",
                details={"content": cleaned_content, "error": str(exc)},
            ) from exc

    async def close(self) -> None:
        """Close underlying HTTP client if internally owned."""
        if not self._external_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OllamaClient":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """Async context manager exit."""
        await self.close()
