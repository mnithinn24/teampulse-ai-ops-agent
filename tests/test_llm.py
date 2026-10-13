"""Unit tests for Ollama client, Gemini client, LLM router, and prompt stripping."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from pydantic import BaseModel

from app.config import get_settings
from app.core.errors import AnalysisError
from app.llm.gemini_client import GeminiClient, clean_json_text
from app.llm.ollama_client import OllamaClient, strip_think_tags
from app.llm.router import LLMRouter
from app.llm.schemas import Analysis, PriorityFlag, QueryIntent


class DummySchema(BaseModel):
    name: str
    score: int


def test_strip_think_tags() -> None:
    """Test stripping <think>...</think> reasoning traces from model responses."""
    raw = "<think>Let me reason about this...</think>{\"name\": \"test\", \"score\": 10}"
    cleaned = strip_think_tags(raw)
    assert cleaned == "{\"name\": \"test\", \"score\": 10}"

    multiline = """<think>
    Step 1: Check inputs.
    Step 2: Format JSON.
    </think>{"name": "done", "score": 100}"""
    assert strip_think_tags(multiline) == '{"name": "done", "score": 100}'

    no_think = '{"name": "plain", "score": 5}'
    assert strip_think_tags(no_think) == no_think


def test_clean_json_text() -> None:
    """Test cleaning markdown backticks from model outputs."""
    with_fence = "```json\n{\"name\": \"fenced\", \"score\": 1}\n```"
    assert clean_json_text(with_fence) == '{"name": "fenced", "score": 1}'

    plain = '{"name": "clean", "score": 2}'
    assert clean_json_text(plain) == plain


@pytest.mark.asyncio
async def test_ollama_client_generate_json_and_think_stripping() -> None:
    """Test Ollama client generates structured JSON and strips thinking tags."""
    ollama_response = {
        "model": "llama3.1:latest",
        "message": {
            "role": "assistant",
            "content": "<think>Thinking trace</think>{\"name\": \"ops\", \"score\": 42}",
        },
        "done": True,
    }

    recorded_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/chat" in str(request.url)
        body = json.loads(request.read().decode("utf-8"))
        recorded_requests.append(body)
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0.1
        assert "format" in body
        return httpx.Response(200, json=ollama_response)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = OllamaClient(
            base_url="http://mock-ollama:11434",
            model="llama3.1:latest",
            client=mock_http,
        )
        res = await client.generate_json(prompt="Test prompt", schema=DummySchema)

    assert isinstance(res, DummySchema)
    assert res.name == "ops"
    assert res.score == 42
    assert len(recorded_requests) == 1


@pytest.mark.asyncio
async def test_ollama_client_health_check() -> None:
    """Test Ollama client health check endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/tags" in str(request.url):
            return httpx.Response(200, json={"models": []})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = OllamaClient(base_url="http://mock-ollama:11434", client=mock_http)
        healthy = await client.check_health()
        assert healthy is True


@pytest.mark.asyncio
async def test_gemini_client_generate_json() -> None:
    """Test Gemini client JSON generation with mocked SDK client."""
    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"name": "gemini_item", "score": 99}'

    mock_genai_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    client = GeminiClient(api_key="fake-key", model="gemini-1.5-pro", client=mock_genai_client)
    res = await client.generate_json(prompt="Generate", schema=DummySchema)

    assert isinstance(res, DummySchema)
    assert res.name == "gemini_item"
    assert res.score == 99


@pytest.mark.asyncio
async def test_router_choice_by_task_type() -> None:
    """Test that router routes simple tasks to Ollama and complex tasks to Gemini."""
    mock_ollama = AsyncMock(spec=OllamaClient)
    mock_ollama.check_health.return_value = True
    mock_ollama.generate_json.return_value = QueryIntent(
        sources_needed=["github"],
        entities=["repo-a"],
        query_type="status",
        time_range="24h",
    )

    mock_gemini = AsyncMock(spec=GeminiClient)
    mock_gemini.generate_json.return_value = Analysis(
        patterns=["Pattern 1"],
        priority_flags=[],
        shipped_this_period=[],
        velocity_note="Good pace",
        one_recommendation="Merge PR",
    )

    router = LLMRouter(ollama_client=mock_ollama, gemini_client=mock_gemini)

    # Simple task -> Routes to Ollama
    res_intent = await router.route("Find status", schema=QueryIntent, task_type="intent")
    assert res_intent.provider == "ollama"
    mock_ollama.generate_json.assert_awaited_once()

    # Complex task -> Routes to Gemini
    res_analysis = await router.route("Analyze work", schema=Analysis, task_type="analysis")
    assert res_analysis.provider == "gemini"
    mock_gemini.generate_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_gemini_failure_ollama_fallback() -> None:
    """Test fallback to Ollama when Gemini fails on an analysis task."""
    mock_gemini = AsyncMock(spec=GeminiClient)
    mock_gemini.generate_json.side_effect = AnalysisError("Gemini rate limited")

    mock_ollama = AsyncMock(spec=OllamaClient)
    mock_ollama.check_health.return_value = True
    mock_ollama.generate_json.return_value = Analysis(
        patterns=["Fallback pattern"],
        priority_flags=[],
        shipped_this_period=[],
        velocity_note="Recovered",
        one_recommendation="Action",
    )

    router = LLMRouter(ollama_client=mock_ollama, gemini_client=mock_gemini)
    res = await router.route("Analyze", schema=Analysis, task_type="analysis")

    assert res.provider == "ollama"
    assert res.data.patterns == ["Fallback pattern"]
    assert res.latency_ms > 0


@pytest.mark.asyncio
async def test_router_ollama_disabled_gemini_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that when Ollama is disabled, router uses Gemini even for simple tasks."""
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
    get_settings.cache_clear()

    mock_ollama = AsyncMock(spec=OllamaClient)
    mock_gemini = AsyncMock(spec=GeminiClient)
    mock_gemini.generate_json.return_value = QueryIntent(
        sources_needed=["jira"],
        entities=["PROJ"],
        query_type="status",
        time_range="7d",
    )

    router = LLMRouter(ollama_client=mock_ollama, gemini_client=mock_gemini)
    res = await router.route("Find issues", schema=QueryIntent, task_type="intent")

    assert res.provider == "gemini"
    mock_ollama.check_health.assert_not_called()
    mock_ollama.generate_json.assert_not_called()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_llm_retry_on_503_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a 503 error is retried with backoff and succeeds on subsequent attempt."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    call_count = 0

    class MockModelResponse:
        text = '{"name": "recovered", "score": 99}'

    class MockModels:
        async def generate_content(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from google.genai.errors import APIError
                raise APIError(code=503, response_json={"error": "503 UNAVAILABLE"})
            return MockModelResponse()

    class MockAio:
        models = MockModels()

    mock_sdk_client = MagicMock()
    mock_sdk_client.aio = MockAio()

    client = GeminiClient(
        api_key="test-key",
        model="gemini-test",
        client=mock_sdk_client,
    )

    result = await client.generate_json("Prompt", DummySchema)
    assert call_count == 2
    assert result.name == "recovered"
    assert result.score == 99


@pytest.mark.asyncio
async def test_llm_no_retry_on_404_fails_immediately() -> None:
    """Test that a 404 error is not retried and fails immediately with AnalysisError."""
    call_count = 0

    class MockModels:
        async def generate_content(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            from google.genai.errors import APIError
            raise APIError(code=404, response_json={"error": "404 NOT_FOUND"})

    class MockAio:
        models = MockModels()

    mock_sdk_client = MagicMock()
    mock_sdk_client.aio = MockAio()

    client = GeminiClient(
        api_key="test-key",
        model="gemini-test",
        client=mock_sdk_client,
    )

    with pytest.raises(AnalysisError) as exc_info:
        await client.generate_json("Prompt", DummySchema)

    # Must fail on the very first attempt without retrying
    assert call_count == 1
    assert "404" in str(exc_info.value)


@pytest.mark.asyncio
async def test_router_fallback_order_gemini_main_to_fallback_to_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test router attempts models in sequence: Gemini main -> Gemini fallback -> Ollama."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-main")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-fallback")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "ollama-local")
    get_settings.cache_clear()

    calls_sequence: list = []

    mock_gemini = AsyncMock(spec=GeminiClient)

    async def mock_gemini_generate(*args, **kwargs):
        model = kwargs.get("model")
        calls_sequence.append(f"gemini:{model}")
        raise AnalysisError(f"Gemini {model} failure")

    mock_gemini.generate_json.side_effect = mock_gemini_generate

    mock_ollama = AsyncMock(spec=OllamaClient)
    mock_ollama.check_health.return_value = True

    async def mock_ollama_generate(*args, **kwargs):
        calls_sequence.append("ollama:ollama-local")
        return Analysis(
            patterns=["Recovered via Ollama"],
            priority_flags=[],
            shipped_this_period=[],
            velocity_note="Note",
            one_recommendation="Rec",
        )

    mock_ollama.generate_json.side_effect = mock_ollama_generate

    router = LLMRouter(ollama_client=mock_ollama, gemini_client=mock_gemini)
    result = await router.route("Analyze work", schema=Analysis, task_type="analysis")

    # Sequence must be: gemini:gemini-main -> gemini:gemini-fallback -> ollama:ollama-local
    assert calls_sequence == [
        "gemini:gemini-main",
        "gemini:gemini-fallback",
        "ollama:ollama-local",
    ]
    assert result.provider == "ollama"
    assert result.data.patterns == ["Recovered via Ollama"]
    get_settings.cache_clear()
