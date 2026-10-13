"""Unit tests for Analyze agent node: prompt synthesis, re-prompting, deduplication, grounding, canonical IDs, and fallback."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.agents.analyze import (
    analyze_workspace,
    build_analysis_prompt,
    build_rule_based_analysis,
)
from app.core.errors import AnalysisError
from app.core.ids import canonical_id, normalize_llm_id
from app.db.database import get_db_session, get_engine, init_db
from app.db.models import Base, Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, FlagEvidence, PriorityFlag


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
    assert "### GITHUB STALE PULL REQUESTS" in prompt
    assert "acme/service#10: Migrate database to v2" in prompt
    assert "1 stale PRs in total, showing the 1 oldest:" in prompt
    assert "### JIRA STALE TICKETS" in prompt
    assert "KAN-42: Investigate memory spike" in prompt
    assert "1 stale Jira issues in total, showing the 1 oldest:" in prompt
    assert "### PREVIOUSLY FLAGGED ITEMS (LAST 24 HOURS)" in prompt
    assert "acme/service#5" in prompt
    assert "IMPORTANT DEDUPLICATION INSTRUCTION" in prompt
    assert "CRITICAL GROUNDING AND EVIDENCE RULES" in prompt
    assert "Copy the exact canonical ID from the data above into item_id" in prompt


def test_normalize_id_variants_map_to_canonical() -> None:
    """Test that all common LLM ID format variants correctly map to canonical IDs."""
    valid_ids = {"psf/requests#6454", "KAN-1", "acme/service#10"}

    # GitHub format variants
    assert normalize_llm_id("psf/requests PR #6454", valid_ids) == "psf/requests#6454"
    assert normalize_llm_id("psf/requests#6454", valid_ids) == "psf/requests#6454"
    assert normalize_llm_id("PR #6454", valid_ids) == "psf/requests#6454"
    assert normalize_llm_id("#6454", valid_ids) == "psf/requests#6454"
    assert normalize_llm_id("pull request 6454", valid_ids) == "psf/requests#6454"
    assert normalize_llm_id("6454", valid_ids) == "psf/requests#6454"

    # Jira format variants
    assert normalize_llm_id("KAN-1", valid_ids) == "KAN-1"
    assert normalize_llm_id("[KAN-1]", valid_ids) == "KAN-1"
    assert normalize_llm_id("ticket KAN-1", valid_ids) == "KAN-1"
    assert normalize_llm_id("issue KAN-1", valid_ids) == "KAN-1"


@pytest.mark.asyncio
async def test_normalize_unknown_id_is_dropped(sample_stale_pr: PullRequest) -> None:
    """Test that truly unknown IDs return None and are dropped from priority flags."""
    valid_ids = {"acme/service#10", "KAN-1"}
    assert normalize_llm_id("unknown/repo#9999", valid_ids) is None
    assert normalize_llm_id("9999", valid_ids) is None
    assert normalize_llm_id("KAN-999", valid_ids) is None

    llm_analysis = Analysis(
        patterns=["Pattern"],
        priority_flags=[
            PriorityFlag(
                title="Unknown item",
                reason="Unknown",
                urgency="urgent",
                source="github",
                item_id="unknown/repo#9999",
                suggested_action="Review",
            )
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Rec",
    )
    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="ollama",
        task_type="analysis",
        latency_ms=10.0,
    )
    state = TeamPulseState(workspace_id="ws-unknown", github_data=[sample_stale_pr])
    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    assert len(analysis.priority_flags) == 0
    assert any("unknown/repo#9999" in err for err in state.errors)


@pytest.mark.asyncio
async def test_normalize_ambiguous_bare_number_is_dropped() -> None:
    """Test that ambiguous bare numbers across multiple repos return None and are dropped."""
    valid_ids = {"repoA#42", "repoB#42", "KAN-1"}
    assert normalize_llm_id("42", valid_ids) is None
    assert normalize_llm_id("PR #42", valid_ids) is None
    assert normalize_llm_id("#42", valid_ids) is None
    # Unambiguous repo-qualified ID succeeds
    assert normalize_llm_id("repoA#42", valid_ids) == "repoA#42"

    pr_a = PullRequest(
        id=42,
        number=42,
        title="PR A",
        author="dev1",
        age_days=10,
        reviewers=[],
        last_commit_days=5,
        is_stale=True,
        repo="repoA",
    )
    pr_b = PullRequest(
        id=42,
        number=42,
        title="PR B",
        author="dev2",
        age_days=12,
        reviewers=[],
        last_commit_days=6,
        is_stale=True,
        repo="repoB",
    )

    llm_analysis = Analysis(
        patterns=["Pattern"],
        priority_flags=[
            PriorityFlag(
                title="Ambiguous PR",
                reason="Bare number",
                urgency="urgent",
                source="github",
                item_id="42",
                suggested_action="Action",
            )
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Rec",
    )
    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="ollama",
        task_type="analysis",
        latency_ms=10.0,
    )
    state = TeamPulseState(workspace_id="ws-ambig", github_data=[pr_a, pr_b])
    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    assert len(analysis.priority_flags) == 0
    assert any("42" in err for err in state.errors)


def test_build_prompt_limits_stale_items_and_shows_totals() -> None:
    """Test prompt limits stale PRs and tickets to 15 items and displays the totals line."""
    prs = [
        PullRequest(
            id=i,
            number=i,
            title=f"PR {i}",
            author="dev",
            age_days=i,
            reviewers=[],
            last_commit_days=i - 2,
            is_stale=True,
            repo="org/repo",
        )
        for i in range(1, 26)  # 25 stale PRs
    ]
    tickets = [
        JiraIssue(
            key=f"KAN-{i}",
            title=f"Ticket {i}",
            status="Open",
            assignee="dev",
            age_days=i,
            days_since_update=i - 3,
            comment_count=0,
            is_stale=True,
            project="KAN",
        )
        for i in range(1, 26)  # 25 stale tickets
    ]

    prompt = build_analysis_prompt(
        workspace_id="ws-limit",
        prs=prs,
        tickets=tickets,
        recent_flags=[],
        stale_pr_days=7,
        stale_ticket_days=14,
        max_items_in_prompt=15,
    )

    # Check totals lines
    assert "25 stale PRs in total, showing the 15 oldest:" in prompt
    assert "25 stale Jira issues in total, showing the 15 oldest:" in prompt

    # Verify oldest item (age 25d) is included, and younger item (e.g. age 10d) is excluded
    assert "org/repo#25: PR 25" in prompt
    assert "org/repo#11: PR 11" in prompt
    assert "org/repo#10: PR 10" not in prompt
    assert "org/repo#1: PR 1" not in prompt

    assert "KAN-25: Ticket 25" in prompt
    assert "KAN-11: Ticket 11" in prompt
    assert "KAN-10: Ticket 10" not in prompt
    assert "KAN-1: Ticket 1" not in prompt


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
                reason="No commits in 10 days by alice",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping bob to complete review",
            )
        ],
        shipped_this_period=["Merged auth bugfix"],
        velocity_note="Moderate velocity",
        one_recommendation="Clear database PR blocker acme/service#10",
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
    assert analysis.one_recommendation == "Clear database PR blocker acme/service#10"

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
                reason="Still stale open 15d by alice",
                urgency="watch",  # Un-escalated 'watch' -> should be skipped!
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping again",
            ),
            PriorityFlag(
                title="Fresh issue",
                reason="New problem open 10d by bob",
                urgency="urgent",
                source="jira",
                item_id="KAN-99",
                suggested_action="Review and reassign",
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
                reason="Inactive 16d assigned to charlie",
                urgency="urgent",
                source="jira",
                item_id="KAN-42",  # Real, in sample_stale_ticket
                suggested_action="Review blockers",
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
                reason="Stalled review 15d by alice",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping bob",
            ),
            PriorityFlag(
                title="Stalled ticket",
                reason="Inactive 16d assigned to charlie",
                urgency="urgent",
                source="jira",
                item_id="KAN-42",
                suggested_action="Check in with charlie",
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


def test_no_example_ids_appear_in_built_prompt() -> None:
    """Test that built prompt contains placeholders and no fake example IDs."""
    prompt = build_analysis_prompt(
        workspace_id="ws-empty",
        prs=[],
        tickets=[],
        recent_flags=[],
        stale_pr_days=7,
        stale_ticket_days=14,
    )

    # Must contain placeholder, never realistic fake IDs
    assert "<copy an ID exactly from the lists above>" in prompt
    assert "acme/service#10" not in prompt
    assert "owner/repo#123" not in prompt
    assert "KAN-123" not in prompt


@pytest.mark.asyncio
async def test_evidence_mismatch_is_corrected(
    sample_stale_pr: PullRequest,
) -> None:
    """Test that evidence with mismatched age or author is corrected against ground truth data."""
    llm_analysis = Analysis(
        patterns=["Pattern"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR",
                reason="Open 15d by alice",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping bob",
                evidence=FlagEvidence(age_days=99, author_or_assignee="bogus_author"),
            )
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Focus on acme/service#10",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="gemini",
        task_type="analysis",
        latency_ms=50.0,
    )

    state = TeamPulseState(
        workspace_id="ws-evidence-mismatch",
        github_data=[sample_stale_pr],
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    flag = analysis.priority_flags[0]
    assert flag.evidence is not None
    # Corrected to real PR age (15) and author ("alice")
    assert flag.evidence.age_days == 15
    assert flag.evidence.author_or_assignee == "alice"


@pytest.mark.asyncio
async def test_unsupported_claim_in_reason_is_replaced(
    sample_stale_pr: PullRequest,
) -> None:
    """Test that unsupported claims like 'security-sensitive' are replaced with real field facts."""
    # sample_stale_pr title is "Migrate database to v2", no security labels
    llm_analysis = Analysis(
        patterns=["Pattern"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR",
                reason="Critical security-sensitive vulnerability and credential exploit detected.",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Review immediately",
            )
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Focus on acme/service#10",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="gemini",
        task_type="analysis",
        latency_ms=50.0,
    )

    state = TeamPulseState(
        workspace_id="ws-unsupported-claim",
        github_data=[sample_stale_pr],
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    flag = analysis.priority_flags[0]
    assert "security" not in flag.reason.lower()
    assert "credential" not in flag.reason.lower()
    assert "Open for 15d by alice with no commits in 10d." in flag.reason


@pytest.mark.asyncio
async def test_abandoned_pr_older_than_180_days_is_never_urgent() -> None:
    """Test that PRs older than 180 days get urgency 'watch' and the standard close inquiry action."""
    abandoned_pr = PullRequest(
        id=99,
        number=99,
        title="Ancient PR",
        author="ancient_author",
        age_days=300,  # > 180 days
        reviewers=[],
        last_commit_days=250,
        is_stale=True,
        repo="acme/service",
    )

    llm_analysis = Analysis(
        patterns=["Backlog pattern"],
        priority_flags=[
            PriorityFlag(
                title="Ancient PR",
                reason="Open for 300d by ancient_author",
                urgency="critical",  # LLM erroneously marked critical!
                source="github",
                item_id="acme/service#99",
                suggested_action="Review and merge immediately",
            )
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Focus on acme/service#99",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="gemini",
        task_type="analysis",
        latency_ms=50.0,
    )

    state = TeamPulseState(
        workspace_id="ws-abandoned",
        github_data=[abandoned_pr],
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    flag = analysis.priority_flags[0]
    assert flag.urgency == "watch"
    assert flag.suggested_action == "Ask the author if still active, otherwise close"


@pytest.mark.asyncio
async def test_recommendation_with_no_id_is_replaced(
    sample_stale_pr: PullRequest,
) -> None:
    """Test that recommendations containing no real canonical IDs are replaced with grounded recommendations."""
    llm_analysis = Analysis(
        patterns=["Pattern"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR",
                reason="Open for 15d by alice",
                urgency="urgent",
                source="github",
                item_id="acme/service#10",
                suggested_action="Ping reviewers",
            )
        ],
        shipped_this_period=[],
        velocity_note="Note",
        one_recommendation="Work diligently on team blockers without naming any tickets.",  # No ID!
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="mock-model",
        provider="gemini",
        task_type="analysis",
        latency_ms=50.0,
    )

    state = TeamPulseState(
        workspace_id="ws-rec-no-id",
        github_data=[sample_stale_pr],
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    # Must be replaced to name the real canonical ID
    assert "acme/service#10" in analysis.one_recommendation
    assert "Focus on unblocking stalled work items: acme/service#10." in analysis.one_recommendation
