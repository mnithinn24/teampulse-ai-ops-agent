"""Unit tests for Analyze agent node: prompt synthesis, re-prompting, deduplication, grounding, and fallback."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.agents.analyze import (
    analyze_workspace,
    build_analysis_prompt,
    build_rule_based_analysis,
)
from app.core.errors import AnalysisError
from app.db.database import get_db_session, get_engine, init_db
from app.db.models import Base, Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, PriorityFlag


@pytest.fixture
def sample_stale_pr() -> PullRequest:
    return PullRequest(
        id=10,
        number=10,
        title="Migrate database to v2",
        author="alice",
        age_days=15,
        reviewers=["bob"],
        last_commit_days=10,
        is_stale=True,
        repo="acme/service",
    )


@pytest.fixture
def sample_stale_ticket() -> JiraIssue:
    return JiraIssue(
        key="KAN-42",
        title="Investigate memory spike",
        status="In Progress",
        assignee="charlie",
        age_days=20,
        days_since_update=16,
        comment_count=1,
        is_stale=True,
        project="KAN",
    )


def test_build_analysis_prompt_normalized_text(
    sample_stale_pr: PullRequest, sample_stale_ticket: JiraIssue
) -> None:
    """Test that prompt is constructed from normalized text sections rather than raw JSON."""
    recent_flags = [
        Flag(
            workspace_id="ws-prompt",
            item_type="github",
            item_id="acme/service#5",
            flag_reason="stale PR",
            flag_level="watch",
        )
    ]

    prompt = build_analysis_prompt(
        workspace_id="ws-prompt",
        prs=[sample_stale_pr],
        tickets=[sample_stale_ticket],
        recent_flags=recent_flags,
        stale_pr_days=7,
        stale_ticket_days=14,
    )

    # Check required text sections
    assert "### ROLE AND DATE" in prompt
    assert "### OPERATIONAL THRESHOLDS" in prompt
    assert "### GITHUB OPEN PULL REQUESTS" in prompt
    assert "acme/service PR #10: Migrate database to v2" in prompt
    assert "### JIRA OPEN TICKETS" in prompt
    assert "[KAN-42] Investigate memory spike" in prompt
    assert "### PREVIOUSLY FLAGGED ITEMS (LAST 24 HOURS)" in prompt
    assert "acme/service#5" in prompt
    assert "IMPORTANT DEDUPLICATION INSTRUCTION" in prompt
    assert "Only reference item IDs that appear in the data. If nothing needs attention, say so. Never invent IDs." in prompt


@pytest.mark.asyncio
async def test_analyze_workspace_valid_json_and_saves_flags(
    sample_stale_pr: PullRequest, sample_stale_ticket: JiraIssue
) -> None:
    """Test successful LLM analysis saves new priority flags to database."""
    valid_analysis = Analysis(
        patterns=["2 items stalled across review and development"],
        priority_flags=[
            PriorityFlag(
                title="Stalled database PR",
                reason="No commits in 10 days",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping reviewers",
            )
        ],
        shipped_this_period=["Merged auth bugfix"],
        velocity_note="Moderate velocity",
        one_recommendation="Clear database PR blocker",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=valid_analysis,
        model_name="mock-gemini",
        provider="gemini",
        task_type="analysis",
        latency_ms=250.0,
    )

    state = TeamPulseState(
        workspace_id="ws-valid-save",
        github_data=[sample_stale_pr],
        jira_data=[sample_stale_ticket],
    )

    analysis, model, latency = await analyze_workspace(state, router=mock_router)

    assert model == "mock-gemini"
    assert latency == 250.0
    assert len(analysis.priority_flags) == 1
    assert analysis.one_recommendation == "Clear database PR blocker"

    # Verify saved flag in database
    with get_db_session() as session:
        saved_flags = list(session.query(Flag).filter(Flag.workspace_id == "ws-valid-save").all())
        assert len(saved_flags) == 1
        assert saved_flags[0].item_id == "acme/service#10"


@pytest.mark.asyncio
async def test_analyze_workspace_reprompts_on_first_failure(
    sample_stale_pr: PullRequest,
) -> None:
    """Test re-prompting once with schema reminder when first attempt returns invalid JSON."""
    recovered_analysis = Analysis(
        patterns=["Recovered pattern"],
        priority_flags=[],
        shipped_this_period=[],
        velocity_note="Recovered",
        one_recommendation="Continue",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    # First call fails, second call succeeds
    mock_router.route.side_effect = [
        AnalysisError("Malformed JSON output"),
        LLMResult(
            data=recovered_analysis,
            model_name="llama3.1:latest",
            provider="ollama",
            task_type="analysis",
            latency_ms=180.0,
        ),
    ]

    state = TeamPulseState(workspace_id="ws-reprompt", github_data=[sample_stale_pr])
    analysis, model, latency = await analyze_workspace(state, router=mock_router)

    assert mock_router.route.call_count == 2
    # Verify second call included the critical schema reminder and grounding instruction
    second_prompt = mock_router.route.call_args_list[1].kwargs["prompt"]
    assert "CRITICAL RE-PROMPT INSTRUCTION" in second_prompt
    assert "You MUST return ONLY valid JSON matching this exact JSON schema" in second_prompt
    assert "Only reference item IDs that appear in the data" in second_prompt

    assert model == "llama3.1:latest"
    assert analysis.patterns == ["Recovered pattern"]


@pytest.mark.asyncio
async def test_analyze_workspace_malformed_twice_uses_rule_based_fallback(
    sample_stale_pr: PullRequest, sample_stale_ticket: JiraIssue
) -> None:
    """Test falling back to rule-based summary when both LLM attempts fail."""
    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.side_effect = [
        AnalysisError("First JSON parse error"),
        AnalysisError("Second JSON parse error"),
    ]

    state = TeamPulseState(
        workspace_id="ws-fallback",
        github_data=[sample_stale_pr],
        jira_data=[sample_stale_ticket],
    )

    analysis, model, latency = await analyze_workspace(state, router=mock_router)

    assert mock_router.route.call_count == 2
    assert model == "rule_based_fallback"
    assert latency == 0.0

    # Rule-based fallback correctly extracted stale items
    assert len(analysis.priority_flags) == 2
    flag_ids = {f.item_id for f in analysis.priority_flags}
    assert "acme/service#10" in flag_ids
    assert "KAN-42" in flag_ids
    assert "rule-based fallback summary" in analysis.velocity_note.lower()


@pytest.mark.asyncio
async def test_analyze_workspace_deduplicates_recent_flags(
    sample_stale_pr: PullRequest,
) -> None:
    """Test that items already flagged within 24 hours are deduplicated unless escalated."""
    # Pre-populate flag in DB
    with get_db_session() as session:
        old_flag = Flag(
            workspace_id="ws-dedup",
            item_type="github",
            item_id="acme/service#10",
            flag_reason="stale",
            flag_level="watch",
        )
        session.add(old_flag)

    valid_fresh_ticket = JiraIssue(
        key="KAN-99",
        title="Fresh urgent bug",
        status="Open",
        assignee="bob",
        age_days=10,
        days_since_update=8,
        comment_count=0,
        is_stale=True,
        project="KAN",
    )

    llm_analysis_with_duplicate = Analysis(
        patterns=["Pattern"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR again",
                reason="Still stale",
                urgency="watch",  # Un-escalated 'watch' -> should be skipped!
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping again",
            ),
            PriorityFlag(
                title="Fresh issue",
                reason="New problem",
                urgency="urgent",
                source="jira",
                item_id="KAN-99",
                suggested_action="Review",
            ),
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Rec",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis_with_duplicate,
        model_name="mock-model",
        provider="gemini",
        task_type="analysis",
        latency_ms=100.0,
    )

    state = TeamPulseState(
        workspace_id="ws-dedup",
        github_data=[sample_stale_pr],
        jira_data=[valid_fresh_ticket],
    )
    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    # acme/service#10 was filtered out due to 24h deduplication
    assert len(analysis.priority_flags) == 1
    assert analysis.priority_flags[0].item_id == "KAN-99"


@pytest.mark.asyncio
async def test_grounding_drops_fake_id_in_priority_flags(
    sample_stale_pr: PullRequest, sample_stale_ticket: JiraIssue
) -> None:
    """Test that priority flags with fake/hallucinated item IDs are dropped and warning added."""
    llm_analysis = Analysis(
        patterns=["Some pattern"],
        priority_flags=[
            PriorityFlag(
                title="Real stalled ticket",
                reason="Inactive",
                urgency="urgent",
                source="jira",
                item_id="KAN-42",  # Real, in sample_stale_ticket
                suggested_action="Review",
            ),
            PriorityFlag(
                title="Fake hallucinated ticket",
                reason="Imaginary",
                urgency="urgent",
                source="jira",
                item_id="KAN-999",  # Fake! Not in collected data
                suggested_action="Investigate",
            ),
        ],
        shipped_this_period=[],
        velocity_note="Normal",
        one_recommendation="Work on KAN-42",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="ollama",
        task_type="analysis",
        latency_ms=100.0,
    )

    state = TeamPulseState(
        workspace_id="ws-grounding-flags",
        github_data=[sample_stale_pr],
        jira_data=[sample_stale_ticket],
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    # Fake flag dropped, real flag preserved
    assert len(analysis.priority_flags) == 1
    assert analysis.priority_flags[0].item_id == "KAN-42"
    # Warning naming the dropped ID added to state.errors
    assert any("KAN-999" in err for err in state.errors)
    assert any("Dropped ungrounded" in err for err in state.errors)


@pytest.mark.asyncio
async def test_grounding_replaces_fake_id_in_recommendation(
    sample_stale_ticket: JiraIssue,
) -> None:
    """Test that fake ID in one_recommendation is replaced with a rule-based sentence referencing real stale items."""
    llm_analysis = Analysis(
        patterns=["Bottleneck pattern"],
        priority_flags=[],
        shipped_this_period=[],
        velocity_note="Velocity steady",
        one_recommendation="Escalate KAN-999 immediately to unblock release.",  # Fake ID!
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="ollama",
        task_type="analysis",
        latency_ms=100.0,
    )

    state = TeamPulseState(
        workspace_id="ws-grounding-rec",
        jira_data=[sample_stale_ticket],  # Real stale ticket is KAN-42
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    assert "KAN-999" not in analysis.one_recommendation
    assert "KAN-42" in analysis.one_recommendation
    assert "Focus on unblocking stalled work items: KAN-42." in analysis.one_recommendation


@pytest.mark.asyncio
async def test_grounding_all_valid_ids_pass_through(
    sample_stale_pr: PullRequest, sample_stale_ticket: JiraIssue
) -> None:
    """Test that all valid IDs in flags, patterns, and recommendations pass through without replacement."""
    llm_analysis = Analysis(
        patterns=["Pattern referencing [KAN-42] and PR #10"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR",
                reason="Stalled review",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping reviewers",
            ),
            PriorityFlag(
                title="Stalled ticket",
                reason="Inactive",
                urgency="urgent",
                source="jira",
                item_id="KAN-42",
                suggested_action="Check in",
            ),
        ],
        shipped_this_period=[],
        velocity_note="Velocity impacted by KAN-42",
        one_recommendation="Focus on acme/service PR #10 and resolve KAN-42.",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="ollama",
        task_type="analysis",
        latency_ms=120.0,
    )

    state = TeamPulseState(
        workspace_id="ws-grounding-valid",
        github_data=[sample_stale_pr],
        jira_data=[sample_stale_ticket],
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    assert len(analysis.priority_flags) == 2
    flag_ids = {f.item_id for f in analysis.priority_flags}
    assert "acme/service#10" in flag_ids
    assert "KAN-42" in flag_ids
    assert analysis.one_recommendation == "Focus on acme/service PR #10 and resolve KAN-42."
    assert analysis.velocity_note == "Velocity impacted by KAN-42"
    assert analysis.patterns == ["Pattern referencing [KAN-42] and PR #10"]
    assert state.errors == []


@pytest.mark.asyncio
async def test_zero_stale_items_skips_llm_call() -> None:
    """Test that when there are zero stale items, LLM call is skipped entirely and clean summary returned."""
    active_pr = PullRequest(
        id=1,
        number=1,
        title="Active feature",
        author="alice",
        age_days=2,
        reviewers=[],
        last_commit_days=1,
        is_stale=False,
        repo="acme/service",
    )
    active_ticket = JiraIssue(
        key="KAN-1",
        title="Active task",
        status="In Progress",
        assignee="bob",
        age_days=3,
        days_since_update=1,
        comment_count=2,
        is_stale=False,
        project="KAN",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    state = TeamPulseState(
        workspace_id="ws-zero-stale",
        github_data=[active_pr],
        jira_data=[active_ticket],
    )

    analysis, model, latency = await analyze_workspace(state, router=mock_router)

    # LLM must NOT be called
    mock_router.route.assert_not_called()
    assert model == "skipped_zero_stale_items"
    assert latency == 0.0
    assert len(analysis.priority_flags) == 0
    assert "No items need immediate attention" in analysis.one_recommendation
