"""LLM router for selecting models based on task complexity, health, and fallback policies."""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import time
from typing import Any, Dict, Generic, List, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel

from app.config import get_settings
from app.core.errors import AnalysisError
from app.llm.gemini_client import GeminiClient
from app.llm.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

SIMPLE_TASK_TYPES = {"intent", "classification", "query_intent", "simple"}

# In-memory tracking of quota-exhausted models (model_name -> unblock unix timestamp)
_model_blocked_until: Dict[str, float] = {}


def is_model_blocked(model_name: str) -> bool:
    """Check if model is currently blocked due to quota exhaustion."""
    until = _model_blocked_until.get(model_name)
    if until is None:
        return False
    if time.time() >= until:
        _model_blocked_until.pop(model_name, None)
        return False
    return True


def block_model(model_name: str, delay_seconds: float) -> None:
    """Block model from being called until delay_seconds elapsed."""
    _model_blocked_until[model_name] = time.time() + max(1.0, float(delay_seconds))


def get_model_blocked_until(model_name: str) -> Optional[float]:
    """Return timestamp when model block expires, or None if unblocked."""
    if is_model_blocked(model_name):
        return _model_blocked_until.get(model_name)
    return None


def clear_blocked_models() -> None:
    """Clear all in-memory blocked model states (for test isolation)."""
    _model_blocked_until.clear()


def parse_duration_string(duration_str: str) -> Optional[float]:
    """Parse duration like '19h23m15s', '68400s', '19h', '10m', '120' into seconds."""
    s = duration_str.strip().rstrip(".,;\"' ")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass

    total_seconds = 0.0
    matched = False
    for val, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", s, re.IGNORECASE):
        matched = True
        num = float(val)
        unit = unit.lower()
        if unit == "d":
            total_seconds += num * 86400.0
        elif unit == "h":
            total_seconds += num * 3600.0
        elif unit == "m":
            total_seconds += num * 60.0
        elif unit == "s":
            total_seconds += num
    if matched:
        return total_seconds
    return None


def extract_retry_delay_seconds(error: Any, default_seconds: float = 68400.0) -> float:
    """Extract retry delay in seconds from a 429 RESOURCE_EXHAUSTED error."""
    err_str = str(error)

    # 1. Look for retryDelay in string or json: e.g. retryDelay: '68400s', "retryDelay": "68400s", retryDelay: 60s
    m_delay = re.search(r'retry[_-]?delay["\']?\s*[:=]\s*["\']?([0-9a-zA-Z\.\s]+?)["\'\,\}\]\n]', err_str, re.IGNORECASE)
    if m_delay:
        parsed = parse_duration_string(m_delay.group(1))
        if parsed is not None and parsed > 0:
            return parsed

    # 2. Look for "Please retry in ..."
    m_retry_in = re.search(r'Please retry in\s+([0-9a-zA-Z\.\s]+?)(?:[\.\,\;\n]|$)', err_str, re.IGNORECASE)
    if m_retry_in:
        parsed = parse_duration_string(m_retry_in.group(1))
        if parsed is not None and parsed > 0:
            return parsed

    # 3. Check details attribute if dict
    if hasattr(error, "details") and isinstance(error.details, dict):
        raw_delay = error.details.get("retryDelay") or error.details.get("retry_delay")
        if raw_delay:
            parsed = parse_duration_string(str(raw_delay))
            if parsed is not None and parsed > 0:
                return parsed

    return default_seconds


def format_delay_duration(seconds: float) -> str:
    """Format seconds into concise human-readable duration (e.g. '19h', '5m', '45s')."""
    if seconds >= 3600:
        hours = round(seconds / 3600)
        return f"{hours}h"
    if seconds >= 60:
        minutes = round(seconds / 60)
        return f"{minutes}m"
    return f"{int(round(seconds))}s"


def is_resource_exhausted_error(exc: Exception) -> bool:
    """Check if exception represents a 429 / RESOURCE_EXHAUSTED quota exhaustion."""
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code is not None:
        try:
            if int(status_code) == 429:
                return True
        except (ValueError, TypeError):
            pass

    if hasattr(exc, "response") and getattr(exc.response, "status_code", None) == 429:
        return True

    if hasattr(exc, "details") and isinstance(exc.details, dict):
        code_in_details = exc.details.get("status_code") or exc.details.get("code")
        if code_in_details is not None:
            try:
                if int(code_in_details) == 429:
                    return True
            except (ValueError, TypeError):
                pass

    err_str = str(exc).upper()
    if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str or "QUOTA EXCEEDED" in err_str:
        return True

    return False


def get_all_models_status() -> Dict[str, Any]:
    """Return status of each configured model, whether it is blocked, and until when."""
    settings = get_settings()
    models: List[Tuple[str, str]] = []
    if settings.gemini_model:
        models.append(("gemini", settings.gemini_model))
    if settings.gemini_fallback_model:
        models.append(("gemini_fallback", settings.gemini_fallback_model))
    if settings.ollama_model:
        models.append(("ollama", settings.ollama_model))

    existing = {m[1] for m in models}
    for m in _model_blocked_until:
        if m not in existing:
            models.append(("gemini", m))

    models_list = []
    models_dict = {}
    for provider, m in models:
        blocked = is_model_blocked(m)
        until_iso = None
        if blocked:
            until_ts = _model_blocked_until.get(m)
            if until_ts:
                until_iso = datetime.fromtimestamp(until_ts, tz=timezone.utc).isoformat()
        info = {
            "model": m,
            "provider": provider,
            "blocked": blocked,
            "blocked_until": until_iso,
            "until": until_iso,
        }
        models_list.append(info)
        models_dict[m] = info

    return {
        "models": models_list,
        **models_dict,
    }


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

    async def get_candidates(self, task_type: str = "analysis") -> List[Tuple[str, str]]:
        """Return prioritized list of unblocked (provider, model_name) candidates.

        Order for complex tasks: Gemini main -> Gemini fallback -> Ollama.
        Order for simple tasks: Ollama -> Gemini main -> Gemini fallback.
        Blocked models (due to 429 quota exhaustion) are automatically skipped.
        """
        settings = get_settings()
        is_simple_task = task_type.lower() in SIMPLE_TASK_TYPES

        ollama_available = False
        if settings.ollama_enabled:
            ollama_available = await self.ollama_client.check_health()

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

        # Skip any model currently blocked by quota exhaustion
        return [(p, m) for p, m in candidates if not is_model_blocked(m)]

    async def call_candidate(
        self,
        provider: str,
        model_name: str,
        prompt: str,
        schema: Type[T],
        task_type: str = "analysis",
        system_prompt: Optional[str] = None,
    ) -> LLMResult[T]:
        """Execute call against a specific candidate model and measure latency."""
        try:
            return await self._call_provider(
                provider=provider,
                prompt=prompt,
                schema=schema,
                task_type=task_type,
                system_prompt=system_prompt,
                model_override=model_name,
            )
        except Exception as exc:
            if is_resource_exhausted_error(exc):
                delay = extract_retry_delay_seconds(exc)
                block_model(model_name, delay)
            raise

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
        - Blocked models are skipped
        - Provider errors are logged once, as one line of at most 200 characters

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
        candidates = await self.get_candidates(task_type=task_type)
        if not candidates:
            raise AnalysisError("No configured LLM providers available (check GEMINI_API_KEY or OLLAMA_BASE_URL).")

        errors: List[str] = []
        for provider, model_name in candidates:
            if is_model_blocked(model_name):
                continue
            try:
                logger.info("Invoking %s (model: %s) for task '%s'", provider, model_name, task_type)
                return await self.call_candidate(
                    provider=provider,
                    model_name=model_name,
                    prompt=prompt,
                    schema=schema,
                    task_type=task_type,
                    system_prompt=system_prompt,
                )
            except Exception as exc:
                if is_resource_exhausted_error(exc):
                    delay = extract_retry_delay_seconds(exc)
                    block_model(model_name, delay)
                    delay_str = format_delay_duration(delay)
                    log_line = f"{model_name} quota exhausted, skipping for {delay_str}"
                else:
                    err_clean = str(exc).replace("\n", " ").replace("\r", " ").strip()
                    # Strip huge response bodies
                    err_clean = re.sub(r"'raw_text':\s*'.*?'", "'raw_text': '...'", err_clean)
                    err_clean = re.sub(r"'response_json':\s*\{.*?\}", "'response_json': {...}", err_clean)
                    prefix = f"Model {model_name} failed: "
                    avail_len = max(0, 200 - len(prefix))
                    if len(err_clean) > avail_len:
                        err_clean = err_clean[: max(0, avail_len - 3)] + "..."
                    log_line = f"{prefix}{err_clean}"

                # Ensure log line is at most 200 characters
                if len(log_line) > 200:
                    log_line = log_line[:200]
                logger.warning(log_line)
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
