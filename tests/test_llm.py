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
