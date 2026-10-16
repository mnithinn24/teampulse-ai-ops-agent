"""Tests for author extraction and filtering in query agent."""

from unittest.mock import AsyncMock
import pytest

from app.agents.query import clear_query_cache, handle_plain_language_query
from app.llm.router import LLMRouter
from evals.fixtures import generate_3_jira_dataset, generate_88_pr_dataset


@pytest.fixture
def mock_clients():
    prs = generate_88_pr_dataset()
    tickets = generate_3_jira_dataset()
    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = tickets
    mock_router = AsyncMock(spec=LLMRouter)
    return mock_gh, mock_jr, mock_router


@pytest.mark.asyncio
async def test_query_count_by_alice(mock_clients) -> None:
    """1. 'How many pull requests by alice?' gives count 15."""
    clear_query_cache()
    mock_gh, mock_jr, mock_router = mock_clients
    res = await handle_plain_language_query(
        question="How many pull requests by alice?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
        workspace_id="test-author-alice",
    )
    assert res["total_count"] == 15
    assert "15" in res["answer"]


@pytest.mark.asyncio
async def test_query_count_does_bob_have_case_insensitive(mock_clients) -> None:
    """2. 'How many PRs does BOB have?' gives 15 (case-insensitive)."""
    clear_query_cache()
    mock_gh, mock_jr, mock_router = mock_clients
    res = await handle_plain_language_query(
        question="How many PRs does BOB have?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
        workspace_id="test-author-bob",
    )
    assert res["total_count"] == 15
    assert "15" in res["answer"]


@pytest.mark.asyncio
async def test_query_count_unknown_author_zed(mock_clients) -> None:
    """3. Unknown author 'zed' gives 'No pull requests found for zed', never 88."""
    clear_query_cache()
    mock_gh, mock_jr, mock_router = mock_clients
    res = await handle_plain_language_query(
        question="How many pull requests by zed?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
        workspace_id="test-author-zed",
    )
    assert res["total_count"] == 0
    assert "No pull requests found for zed" in res["answer"]
    assert "88" not in res["answer"]


@pytest.mark.asyncio
async def test_query_count_no_author_gives_88(mock_clients) -> None:
    """4. A question with no author still gives 88."""
    clear_query_cache()
    mock_gh, mock_jr, mock_router = mock_clients
    res = await handle_plain_language_query(
        question="How many pull requests are there?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
        workspace_id="test-author-none",
    )
    assert res["total_count"] == 88
    assert "88" in res["answer"]
