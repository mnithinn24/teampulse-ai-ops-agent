"""Unit tests for LangGraph pipeline execution and resilient data collection."""

from typing import List
from unittest.mock import AsyncMock, patch
import pytest

from app.core.errors import GitHubError, JiraError
from app.graph.pipeline import create_pipeline, run_collect
from app.graph.state import TeamPulseState
from app.integrations.github_client import GitHubClient, PullRequest
from app.integrations.jira_client import JiraClient, JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, PriorityFlag


@pytest.fixture
def mock_pr() -> PullRequest:
    return PullRequest(
        id=1,
        number=1,
        title="Add auth middleware",
        author="alice",
        age_days=3,
        reviewers=["bob"],
        last_commit_days=1,
        is_stale=False,
        repo="org/repo",
    )


@pytest.fixture
def mock_issue() -> JiraIssue:
    return JiraIssue(
        key="PROJ-10",
        title="Resolve payment webhook timeout",
        status="In Progress",
        assignee="carol",
        age_days=16,
        days_since_update=15,
        comment_count=2,
        is_stale=True,
        project="PROJ",
    )


@pytest.fixture
def mock_router() -> LLMRouter:
    router = AsyncMock(spec=LLMRouter)
    router.route.return_value = LLMResult(
        data=Analysis(
            patterns=["1 bottleneck identified"],
            priority_flags=[
                PriorityFlag(
                    title="Stale payment ticket",
                    reason="Inactive for 15 days",
                    urgency="urgent",
                    source="jira",
                    item_id="PROJ-10",
                    suggested_action="Review with assignee",
                )
            ],
            shipped_this_period=[],
            velocity_note="Sprint on track",
            one_recommendation="Clear payment blocker",
        ),
        model_name="mock-model",
        provider="mock",
        task_type="analysis",
        latency_ms=15.0,
    )
    return router


@pytest.mark.asyncio
async def test_pipeline_full_success(
    mock_pr: PullRequest, mock_issue: JiraIssue, mock_router: LLMRouter
) -> None:
    """Test standard pipeline execution where both GitHub and Jira succeed."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.return_value = [mock_pr]
    mock_gh.close = AsyncMock()

    mock_jr = AsyncMock(spec=JiraClient)
    mock_jr.fetch_open_issues.return_value = [mock_issue]
    mock_jr.close = AsyncMock()

    pipeline = create_pipeline(github_client=mock_gh, jira_client=mock_jr, router=mock_router)
    initial_state = TeamPulseState(workspace_id="test-ws", run_type="morning")

    result = await pipeline.ainvoke(initial_state)

    state = result if isinstance(result, TeamPulseState) else TeamPulseState(**result)
    assert len(state.github_data) == 1
    assert state.github_data[0].id == 1  # type: ignore

    assert len(state.jira_data) == 1
    assert state.jira_data[0].key == "PROJ-10"  # type: ignore

    assert state.analysis is not None
    assert len(state.priority_items) > 0
    assert state.briefing is not None
    assert len(state.errors) == 0


@pytest.mark.asyncio
async def test_pipeline_jira_fails_github_succeeds(
    mock_pr: PullRequest, mock_router: LLMRouter
) -> None:
    """Test resilient collection when Jira fails: Jira marked unavailable, pipeline continues."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.return_value = [mock_pr]
    mock_gh.close = AsyncMock()

    mock_jr = AsyncMock(spec=JiraClient)
    mock_jr.fetch_open_issues.side_effect = JiraError("Jira Cloud unreachable")
    mock_jr.close = AsyncMock()

    pipeline = create_pipeline(github_client=mock_gh, jira_client=mock_jr, router=mock_router)
    initial_state = TeamPulseState(workspace_id="test-ws", run_type="ondemand")

    result = await pipeline.ainvoke(initial_state)

    state = result if isinstance(result, TeamPulseState) else TeamPulseState(**result)

    # GitHub succeeded
    assert len(state.github_data) == 1

    # Jira marked unavailable
    assert isinstance(state.jira_data, dict)
    assert state.jira_data.get("status") == "unavailable"
    assert "Jira Cloud unreachable" in state.jira_data.get("error", "")

    # Errors recorded
    assert len(state.errors) == 1
    assert "Jira collection unavailable" in state.errors[0]

    # Pipeline still completed Analyze and Act
    assert state.analysis is not None
    assert state.briefing is not None


@pytest.mark.asyncio
async def test_pipeline_github_fails_jira_succeeds(
    mock_issue: JiraIssue, mock_router: LLMRouter
) -> None:
    """Test resilient collection when GitHub fails: GitHub marked unavailable, pipeline continues."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.side_effect = GitHubError("Bad credentials")
    mock_gh.close = AsyncMock()

    mock_jr = AsyncMock(spec=JiraClient)
    mock_jr.fetch_open_issues.return_value = [mock_issue]
    mock_jr.close = AsyncMock()

    pipeline = create_pipeline(github_client=mock_gh, jira_client=mock_jr, router=mock_router)
    initial_state = TeamPulseState(workspace_id="test-ws", run_type="evening")

    result = await pipeline.ainvoke(initial_state)

    state = result if isinstance(result, TeamPulseState) else TeamPulseState(**result)

    # GitHub marked unavailable
    assert isinstance(state.github_data, dict)
    assert state.github_data.get("status") == "unavailable"

    # Jira succeeded
    assert len(state.jira_data) == 1

    # Errors recorded
    assert len(state.errors) == 1
    assert "GitHub collection unavailable" in state.errors[0]

    # Pipeline continued
    assert state.analysis is not None
    assert state.briefing is not None


@pytest.mark.asyncio
async def test_pipeline_both_sources_fail(mock_router: LLMRouter) -> None:
    """Test resilient collection when both external integrations fail."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.side_effect = GitHubError("GitHub network timeout")
    mock_gh.close = AsyncMock()

    mock_jr = AsyncMock(spec=JiraClient)
    mock_jr.fetch_open_issues.side_effect = JiraError("Jira gateway timeout")
    mock_jr.close = AsyncMock()

    pipeline = create_pipeline(github_client=mock_gh, jira_client=mock_jr, router=mock_router)
    initial_state = TeamPulseState(workspace_id="test-ws", run_type="morning")

    result = await pipeline.ainvoke(initial_state)

    state = result if isinstance(result, TeamPulseState) else TeamPulseState(**result)

    assert isinstance(state.github_data, dict)
    assert state.github_data.get("status") == "unavailable"

    assert isinstance(state.jira_data, dict)
    assert state.jira_data.get("status") == "unavailable"

    assert len(state.errors) == 2
    assert state.analysis is not None
    assert state.briefing is not None


@pytest.mark.asyncio
async def test_collect_node_uses_repos_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that Collect node reads GITHUB_REPOS and JIRA_PROJECT_KEY from settings."""
    from app.config import get_settings

    monkeypatch.setenv("GITHUB_REPOS", "custom-org/service-alpha,custom-org/service-beta")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "KAN")
    get_settings.cache_clear()

    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.return_value = []
    mock_gh.close = AsyncMock()

    mock_jr = AsyncMock(spec=JiraClient)
    mock_jr.fetch_open_issues.return_value = []
    mock_jr.close = AsyncMock()

    state = TeamPulseState(workspace_id="test-ws")
    await run_collect(state, github_client=mock_gh, jira_client=mock_jr)

    # Verify Collect node passed configured settings to clients
    mock_gh.fetch_all_open_prs.assert_awaited_once_with(
        repos=["custom-org/service-alpha", "custom-org/service-beta"]
    )
    mock_jr.fetch_open_issues.assert_awaited_once_with(project_key="KAN")
    get_settings.cache_clear()
