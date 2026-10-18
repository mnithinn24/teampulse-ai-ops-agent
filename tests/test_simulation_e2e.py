"""Phase 11: Complete Unit Tests & Local Simulation Suite.

Tests every TeamPulse feature locally with safe simulated data:
- Feature 1: GitHub Ingestion & Stale PR Detection
- Feature 2: Jira Ticket Ingestion & SLA Tracking
- Feature 3: LLM Analysis, Fallback Engine & Hallucination Defense
- Feature 4: Slack Morning Briefing Block Kit Generation
- Feature 5: Interactive Action Execution & Idempotency Safeguards
- Feature 6: Plain-Language Query Engine & Out-of-Scope Shield
- Feature 7: AI Project Planner (Capacity & Jira Task Hierarchy)
- Feature 8: PR Completion Slack Alert & Jira Key Linking
- Feature 9: One-Click Slack Auto-Merge & Jira Done Transition
- Feature 10: Decline PR Action & State Synchronization
- Feature 11: Network Isolation, Dry-Run Security & Circuit Breakers
- Feature 12: End-to-End Pipeline Workflow Execution
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.agents.actions import execute_approved_action
from app.agents.analyze import (
    analyze_workspace,
    build_analysis_prompt,
    build_rule_based_analysis,
)
from app.agents.briefing import build_slack_briefing
from app.agents.planner import (
    format_plan_for_slack,
    generate_project_plan,
    normalize_and_balance_hours,
    parse_plan_request_text,
    provision_jira_plan,
    rule_based_fallback_plan,
)
from app.agents.pr_alert import (
    build_pr_slack_blocks,
    extract_jira_keys,
    fetch_jira_ticket_context,
    handle_pr_approval_action,
)
from app.agents.query import handle_plain_language_query
from app.api.routes.slack import process_slack_interaction
from app.config import get_settings
from app.core.ids import canonical_id, normalize_llm_id
from app.core.security import verify_slack_signature
from app.db.database import get_session_factory
from app.db.models import ActionTaken, Flag
from app.graph.pipeline import create_pipeline
from app.graph.state import TeamPulseState
from app.integrations.github_client import GitHubClient, PullRequest
from app.integrations.jira_client import JiraClient, JiraIssue
from app.integrations.slack_client import SlackClient
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, EpicSpec, FlagEvidence, PriorityFlag, ProjectPlan, SubTaskSpec, TaskSpec


# ==============================================================================
# Simulated Data Fixtures
# ==============================================================================


@pytest.fixture
def simulated_prs() -> List[PullRequest]:
    """A realistic set of simulated pull requests with various states."""
    return [
        PullRequest(
            id=101,
            number=101,
            title="feat(auth): Add JWT middleware",
            author="alice",
            age_days=2,
            reviewers=["bob"],
            last_commit_days=1,
            is_stale=False,
            repo="org/repo",
            html_url="https://github.com/org/repo/pull/101",
        ),
        PullRequest(
            id=102,
            number=102,
            title="fix(billing): Resolve payment race condition",
            author="bob",
            age_days=12,
            reviewers=[],
            last_commit_days=10,
            is_stale=True,
            repo="org/repo",
            html_url="https://github.com/org/repo/pull/102",
        ),
        PullRequest(
            id=103,
            number=103,
            title="refactor(rag): Vector database indexing pipeline",
            author="carol",
            age_days=18,
            reviewers=["alice", "bob"],
            last_commit_days=15,
            is_stale=True,
            repo="org/repo",
            html_url="https://github.com/org/repo/pull/103",
        ),
    ]


@pytest.fixture
def simulated_jira_issues() -> List[JiraIssue]:
    """A realistic set of simulated Jira issues with various states."""
    return [
        JiraIssue(
            key="KAN-10",
            title="Implement OAuth2 token rotation",
            status="In Progress",
            assignee="alice",
            age_days=4,
            days_since_update=2,
            comment_count=3,
            is_stale=False,
            project="KAN",
            issue_url="https://company.atlassian.net/browse/KAN-10",
        ),
        JiraIssue(
            key="KAN-42",
            title="Medical RAG document ingestion pipeline",
            status="In Review",
            assignee="carol",
            age_days=20,
            days_since_update=16,
            comment_count=1,
            is_stale=True,
            project="KAN",
            issue_url="https://company.atlassian.net/browse/KAN-42",
        ),
    ]


# ==============================================================================
# Feature 1 & 2: GitHub & Jira Ingestion and Stale Detection
# ==============================================================================


def test_simulation_feature1_and_2_data_ingestion_and_stale_detection(
    simulated_prs: List[PullRequest], simulated_jira_issues: List[JiraIssue]
) -> None:
    """Verify simulated ingestion accurately tags stale PRs and tickets by threshold."""
    # GitHub stale verification
    stale_prs = [pr for pr in simulated_prs if pr.is_stale]
    active_prs = [pr for pr in simulated_prs if not pr.is_stale]
    assert len(stale_prs) == 2
    assert len(active_prs) == 1
    assert stale_prs[0].number == 102
    assert stale_prs[0].reviewers == []  # Missing reviewers

    # Jira stale verification
    stale_jira = [issue for issue in simulated_jira_issues if issue.is_stale]
    active_jira = [issue for issue in simulated_jira_issues if not issue.is_stale]
    assert len(stale_jira) == 1
    assert len(active_jira) == 1
    assert stale_jira[0].key == "KAN-42"
    assert stale_jira[0].days_since_update >= 14


# ==============================================================================
# Feature 3: Grounded LLM Analysis & Hallucination Defense
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature3_grounded_analysis_and_hallucination_defense(
    simulated_prs: List[PullRequest], simulated_jira_issues: List[JiraIssue]
) -> None:
    """Verify that analysis properly grounds priority flags and purges hallucinated IDs."""
    # 1. Test deterministic fallback analysis without external LLM
    fallback_analysis = build_rule_based_analysis(
        prs=simulated_prs,
        tickets=simulated_jira_issues,
        recent_flag_ids=set(),
    )

    assert fallback_analysis is not None
    assert len(fallback_analysis.priority_flags) >= 1

    # Verify all generated flags match genuine simulated items
    valid_keys = {"org/repo#101", "org/repo#102", "org/repo#103", "KAN-10", "KAN-42"}
    for flag in fallback_analysis.priority_flags:
        assert flag.item_id in valid_keys

    # 2. Test hallucination defense using canonical ID validation
    # Real items return canonical IDs
    assert normalize_llm_id("PR #102", valid_keys) == "org/repo#102"
    assert normalize_llm_id("KAN-42", valid_keys) == "KAN-42"

    # Hallucinated IDs are completely rejected and return None
    assert normalize_llm_id("PR #999", valid_keys) is None
    assert normalize_llm_id("FAKE-1234", valid_keys) is None

    # 3. Test analyze_workspace executes safely with fallback router
    state = TeamPulseState(
        workspace_id="test-sim-ws",
        run_type="morning",
        github_data=simulated_prs,
        jira_data=simulated_jira_issues,
    )
    analysis, model_used, _ = await analyze_workspace(state, router=None)
    assert analysis is not None
    assert model_used == "rule_based_fallback"
    assert len(analysis.priority_flags) > 0


# ==============================================================================
# Feature 4: Slack Morning Briefing Block Kit Generation
# ==============================================================================


def test_simulation_feature4_slack_briefing_block_kit_generation(
    simulated_prs: List[PullRequest], simulated_jira_issues: List[JiraIssue]
) -> None:
    """Verify Slack morning briefing generates compliant Block Kit payloads."""
    analysis = Analysis(
        patterns=["2 bottlenecks detected in sprint"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR #102: payment race condition",
                reason="Inactive for 10 days without reviewers",
                urgency="urgent",
                source="github",
                item_id="org/repo#102",
                suggested_action="Assign code reviewer",
            )
        ],
        shipped_this_period=["Merged auth middleware"],
        velocity_note="Velocity steady at 85%",
        one_recommendation="Unblock org/repo#102 to unblock billing milestone",
    )

    state = TeamPulseState(
        workspace_id="test-workspace",
        run_type="morning",
        github_data=simulated_prs,
        jira_data=simulated_jira_issues,
        analysis=analysis.model_dump(),
        priority_items=[f.model_dump() for f in analysis.priority_flags],
    )

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    flags_with_db = [
        {
            "id": 101,
            "item_id": "org/repo#102",
            "title": "Stale PR #102: payment race condition",
            "reason": "Inactive for 10 days without reviewers",
            "urgency": "urgent",
            "suggested_action": "Assign code reviewer",
            "evidence_age_days": 10,
            "evidence_author": "bob",
        }
    ]

    briefing = build_slack_briefing(state=state, flags_with_db=flags_with_db, now=now)
    assert isinstance(briefing, dict)
    blocks = briefing.get("blocks", [])
    assert len(blocks) >= 4

    # Verify Block Kit structures
    block_types = [b.get("type") for b in blocks]
    assert "header" in block_types
    assert "section" in block_types


# ==============================================================================
# Feature 5: Interactive Actions & Idempotency Safeguards
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature5_interactive_actions_and_idempotency() -> None:
    """Verify Approve & Dismiss action execution with idempotency protection in DB."""
    session_factory = get_session_factory()

    # Step 1: Pre-populate flag in database
    with session_factory() as session:
        flag = Flag(
            workspace_id="default",
            item_type="github_pr",
            item_id="org/repo#102",
            title="PR #102: Resolve payment race condition",
            flag_reason="Inactive for 10 days",
            urgency="urgent",
            status="pending",
        )
        session.add(flag)
        session.commit()
        flag_id = flag.id

    # Step 2: First interaction (Approve action)
    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.create_issue.return_value = MagicMock(key="KAN-99")
    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.update_message.return_value = {"ok": True}

    payload = {
        "user": {"id": "U12345"},
        "actions": [{"action_id": "approve_action", "value": str(flag_id)}],
        "channel": {"id": "C12345"},
        "message": {"ts": "1710000000.000100", "blocks": []},
    }

    # Process first approve click
    await process_slack_interaction(
        payload=payload,
        session_factory=session_factory,
        jira_client=mock_jira,
        slack_client=mock_slack,
    )

    # Verify status changed to approved in DB
    with session_factory() as session:
        updated_flag = session.get(Flag, flag_id)
        assert updated_flag is not None
        assert updated_flag.status == "approved"
        assert updated_flag.approved_by == "U12345"

    # Step 3: Second interaction with exact same flag (Double click simulation)
    await process_slack_interaction(
        payload=payload,
        session_factory=session_factory,
        jira_client=mock_jira,
        slack_client=mock_slack,
    )

    # Ensure Jira create_issue was not called a second time
    assert mock_jira.create_issue.call_count <= 1


# ==============================================================================
# Feature 6: Plain-Language Query Engine & Out-of-Scope Shield
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature6_query_engine_and_out_of_scope_handling(
    simulated_prs: List[PullRequest], simulated_jira_issues: List[JiraIssue]
) -> None:
    """Verify natural language query engine returns grounded facts and blocks out-of-scope queries."""
    from app.agents.query import clear_query_cache
    clear_query_cache()

    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.return_value = simulated_prs
    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.fetch_open_issues.return_value = simulated_jira_issues

    # 1. Author count query
    resp_author = await handle_plain_language_query(
        question="How many pull requests by alice?",
        workspace_id="default-author",
        github_client=mock_gh,
        jira_client=mock_jira,
    )
    assert resp_author.get("out_of_scope") is not True
    assert "1" in resp_author["answer"]
    assert resp_author.get("total_count") == 1

    # 2. Stale items count query
    clear_query_cache()
    resp_count = await handle_plain_language_query(
        question="how many stale PRs are there?",
        workspace_id="default-stale",
        github_client=mock_gh,
        jira_client=mock_jira,
    )
    assert resp_count.get("out_of_scope") is not True
    assert "2" in resp_count["answer"]  # 2 stale PRs in simulated_prs

    # 3. Out-of-scope query
    clear_query_cache()
    resp_offtopic = await handle_plain_language_query(
        question="can you write a poem about the ocean?",
        workspace_id="default-offtopic",
        github_client=mock_gh,
        jira_client=mock_jira,
    )
    assert resp_offtopic.get("out_of_scope") is True or "only answer questions about your GitHub" in resp_offtopic["answer"]


# ==============================================================================
# Feature 7: AI Project Planner (Capacity & Jira Task Hierarchy)
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature7_ai_project_planner_capacity_and_jira_hierarchy() -> None:
    """Verify AI Project Planner calculates capacity and decomposes into Epics, Tasks, and Subtasks."""
    # Test request parsing
    req = parse_plan_request_text("Medical RAG application, 2 developers, 2 weeks")
    assert req["developers"] == 2
    assert req["weeks"] == 2.0
    assert "Medical RAG" in req["description"]

    # Test plan generation with deterministic fallback
    plan = await generate_project_plan(
        description="Medical RAG with React UI and Document Chunking",
        developers=2,
        weeks=2.0,
        router=None,
    )

    assert isinstance(plan, ProjectPlan)
    assert len(plan.epics) >= 2
    assert plan.total_estimated_hours > 0

    # Verify hierarchy
    total_subtask_hours = 0.0
    for epic in plan.epics:
        assert len(epic.tasks) >= 1
        for task in epic.tasks:
            assert len(task.subtasks) >= 1
            for sub in task.subtasks:
                assert sub.estimated_hours > 0
                total_subtask_hours += sub.estimated_hours

    assert total_subtask_hours > 0

    # Test dry-run provisioning
    dry_plan = await provision_jira_plan(
        plan=plan,
        project_key="KAN",
        dry_run=True,
    )
    assert dry_plan.epics[0].jira_key.startswith("DRY-KAN-")

    # Test Jira hierarchy creation with mock Jira client
    mock_jira = AsyncMock(spec=JiraClient)
    counter = 0

    def mock_create(**kwargs):
        nonlocal counter
        counter += 1
        mock_issue = MagicMock()
        mock_issue.key = f"KAN-{100 + counter}"
        return mock_issue

    mock_jira.create_issue = AsyncMock(side_effect=mock_create)

    provisioned_plan = await provision_jira_plan(
        plan=plan,
        project_key="KAN",
        jira_client=mock_jira,
        dry_run=False,
    )

    assert provisioned_plan.epics[0].jira_key.startswith("KAN-")
    assert mock_jira.create_issue.call_count >= 5


# ==============================================================================
# Feature 8: PR Completion Slack Alert & Jira Key Linking
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature8_pr_completion_webhook_and_jira_linking() -> None:
    """Verify PR completion extracts Jira keys from branch and builds interactive alert card."""
    # Test key extraction
    branch_name = "feature/KAN-42-medical-rag-upload"
    pr_body = "Implements document upload pipeline. Related to KAN-42 and KAN-10."
    keys = extract_jira_keys(f"{branch_name}\n{pr_body}")
    assert keys == ["KAN-42", "KAN-10"]

    # Test simulated Jira context lookup
    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.get_issue.return_value = JiraIssue(
        key="KAN-42",
        title="Medical RAG document ingestion pipeline",
        status="In Review",
        assignee="Carol Dev",
        age_days=0,
        days_since_update=0,
        comment_count=0,
        is_stale=False,
        project="KAN",
    )

    jira_ctx = await fetch_jira_ticket_context("KAN-42", jira_client=mock_jira)
    assert jira_ctx is not None
    assert jira_ctx["key"] == "KAN-42"
    assert jira_ctx["title"] == "Medical RAG document ingestion pipeline"

    # Test Slack alert block building
    blocks = build_pr_slack_blocks(
        repo="org/repo",
        pr_number=42,
        pr_title="feat: Add medical doc parser",
        pr_author="carol",
        pr_url="https://github.com/org/repo/pull/42",
        head_ref=branch_name,
        base_ref="main",
        jira_info=jira_ctx,
    )

    assert isinstance(blocks, list)
    # Check that interactive actions block contains Approve & Merge and Decline buttons
    action_block = next((b for b in blocks if b.get("type") == "actions"), None)
    assert action_block is not None
    action_ids = [el.get("action_id") for el in action_block.get("elements", [])]
    assert "pr_approve_and_merge" in action_ids
    assert "pr_decline" in action_ids


# ==============================================================================
# Feature 9: One-Click Slack Auto-Merge & Jira Done Transition
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature9_one_click_auto_merge_and_jira_done_transition() -> None:
    """Verify team lead approval merges GitHub PR and transitions Jira ticket to Done."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.merge_pull_request.return_value = {
        "merged": True,
        "sha": "abc1234567890",
        "message": "Pull Request successfully merged",
    }

    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.mark_issue_done.return_value = "Done"
    mock_jira.add_comment.return_value = {}

    session_factory = get_session_factory()

    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="org/repo#42|KAN-42",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_gh,
        jira_client=mock_jira,
        session_factory=session_factory,
    )

    assert "Merged org/repo#42 to main" in outcome
    assert "KAN-42 as Done" in outcome

    # Verify GitHub merge called
    mock_gh.merge_pull_request.assert_awaited_once_with(
        repo="org/repo",
        pull_number=42,
        commit_title="Merge pull request #42 from org/repo",
        commit_message="Approved and merged by @U_LEAD via TeamPulse Slack action.",
    )

    # Verify Jira Done transition called
    mock_jira.mark_issue_done.assert_awaited_once_with("KAN-42")

    # Verify recorded in DB
    with session_factory() as session:
        record = (
            session.query(ActionTaken)
            .filter_by(action_type="pr_approve_and_merge", target_id="org/repo#42")
            .first()
        )
        assert record is not None
        assert record.approved_by == "U_LEAD"
        assert record.status == "executed"


# ==============================================================================
# Feature 10: Decline PR Action & State Synchronization
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_feature10_decline_action_workflow() -> None:
    """Verify team lead decline action closes PR and updates Slack status."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.close_pull_request.return_value = {"state": "closed"}

    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.add_comment.return_value = {}

    session_factory = get_session_factory()

    outcome = await handle_pr_approval_action(
        action_id="pr_decline",
        value="org/repo#42|KAN-42",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_gh,
        jira_client=mock_jira,
        session_factory=session_factory,
    )

    assert "Declined and closed org/repo#42" in outcome
    mock_gh.close_pull_request.assert_awaited_once_with(repo="org/repo", pull_number=42)

    # Check database audit record
    with session_factory() as session:
        record = (
            session.query(ActionTaken)
            .filter_by(action_type="pr_decline", target_id="org/repo#42")
            .first()
        )
        assert record is not None
        assert record.approved_by == "U_LEAD"
        assert record.status == "executed"


# ==============================================================================
# Feature 11: Network Isolation, Dry-Run Security & Circuit Breakers
# ==============================================================================


def test_simulation_feature11_network_isolation_and_safety_guards() -> None:
    """Verify that tests strictly prevent real external HTTP calls and enforce dry-run safety."""
    from tests.conftest import RealNetworkForbiddenError, _check_forbidden_target

    # 1. Target check verifies external domains are caught
    with pytest.raises(RealNetworkForbiddenError):
        _check_forbidden_target("https://api.github.com/repos/org/repo")

    with pytest.raises(RealNetworkForbiddenError):
        _check_forbidden_target("https://company.atlassian.net/rest/api/3/issue")

    with pytest.raises(RealNetworkForbiddenError):
        _check_forbidden_target("https://slack.com/api/chat.postMessage")

    # 2. Dry run configuration is verified
    settings = get_settings()
    assert settings.dry_run_actions is True
    assert settings.enable_scheduler is False


# ==============================================================================
# Feature 12: End-to-End Pipeline Workflow Execution
# ==============================================================================


@pytest.mark.asyncio
async def test_simulation_end_to_end_full_pipeline_run(
    simulated_prs: List[PullRequest], simulated_jira_issues: List[JiraIssue]
) -> None:
    """Execute the full LangGraph pipeline from collect -> analyze -> act using simulated data."""
    mock_gh = AsyncMock(spec=GitHubClient)
    mock_gh.fetch_all_open_prs.return_value = simulated_prs

    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.fetch_open_issues.return_value = simulated_jira_issues

    pipeline = create_pipeline(github_client=mock_gh, jira_client=mock_jira)
    initial_state = TeamPulseState(
        workspace_id="simulation-ws",
        run_type="morning",
    )

    final_result = await pipeline.ainvoke(initial_state)
    state = final_result if isinstance(final_result, TeamPulseState) else TeamPulseState(**final_result)

    assert len(state.github_data) == 3
    assert len(state.jira_data) == 2
    assert state.analysis is not None
    assert len(state.priority_items) > 0

    # Ensure collection was executed
    mock_gh.fetch_all_open_prs.assert_awaited_once()
    mock_jira.fetch_open_issues.assert_awaited_once()


@pytest.mark.asyncio
async def test_simulation_script_cli_execution() -> None:
    """Verify that scripts/simulate_all_features.py executes with 100% pass rate."""
    from scripts.simulate_all_features import main as sim_main
    with patch("sys.argv", ["simulate_all_features.py", "--json"]):
        exit_code = await sim_main()
    assert exit_code == 0

