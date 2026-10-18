"""Tests for Phase 8: AI Project Planner, hour balancing, and Jira provisioning."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.agents.planner import (
    format_plan_for_slack,
    generate_project_plan,
    normalize_and_balance_hours,
    parse_plan_request_text,
    provision_jira_plan,
    rule_based_fallback_plan,
)
from app.api.main import app
from app.config import Settings
from app.db.database import get_session_factory
from app.db.models import ActionTaken
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import EpicSpec, ProjectPlan, SubTaskSpec, TaskSpec


@pytest.fixture
def mock_jira_issue_factory():
    def _create(key: str, summary: str):
        return JiraIssue(
            key=key,
            title=summary,
            status="Open",
            assignee="developer",
            age_days=0,
            days_since_update=0,
            comment_count=0,
            is_stale=False,
            project="KAN",
            url=f"https://jira.test/browse/{key}",
        )
    return _create


def test_parse_plan_request_text():
    """Test natural language parameter parsing."""
    # Standard format
    r1 = parse_plan_request_text("Medical RAG, 2 developers, 2 weeks")
    assert r1["developers"] == 2
    assert r1["weeks"] == 2.0
    assert "Medical RAG" in r1["description"]

    # Short prefix format
    r2 = parse_plan_request_text("3 devs, 1.5 wks: Customer Support Copilot")
    assert r2["developers"] == 3
    assert r2["weeks"] == 1.5
    assert "Customer Support Copilot" in r2["description"]

    # Days duration
    r3 = parse_plan_request_text("Internal tooling, 1 engineer, 10 days")
    assert r3["developers"] == 1
    assert r3["weeks"] == 2.0
    assert "Internal tooling" in r3["description"]

    # Default fallback when missing
    r4 = parse_plan_request_text("Simple Document Search")
    assert r4["developers"] == 2
    assert r4["weeks"] == 2.0
    assert "Simple Document Search" in r4["description"]


def test_rule_based_fallback_plan():
    """Test deterministic rule-based plan generation and capacity calculation."""
    plan = rule_based_fallback_plan(
        description="Medical RAG with React UI",
        developers=2,
        weeks=2.0,
    )

    assert plan.developers_count == 2
    assert plan.weeks_duration == 2.0
    assert plan.total_capacity_hours == 160
    assert plan.total_estimated_hours == 160
    assert len(plan.epics) == 4

    # Verify hierarchy
    total_epic_hours = 0
    for epic in plan.epics:
        total_epic_hours += epic.estimated_hours
        task_sum = sum(t.estimated_hours for t in epic.tasks)
        assert epic.estimated_hours == task_sum
        for task in epic.tasks:
            subtask_sum = sum(st.estimated_hours for st in task.subtasks)
            assert task.estimated_hours == subtask_sum
            assert len(task.subtasks) >= 2

    assert total_epic_hours == 160


def test_normalize_and_balance_hours():
    """Test hours normalization when LLM returns unbalanced or missing subtasks."""
    raw_plan = ProjectPlan(
        project_name="Test Project",
        summary="A test summary",
        developers_count=1,
        weeks_duration=1.0,
        total_capacity_hours=40,
        total_estimated_hours=0,
        epics=[
            EpicSpec(
                title="Epic 1",
                description="First epic",
                estimated_hours=40,
                tasks=[
                    TaskSpec(
                        title="Task 1",
                        description="Task without subtasks",
                        estimated_hours=40,
                        subtasks=[],
                    )
                ],
            )
        ],
    )

    balanced = normalize_and_balance_hours(raw_plan, developers=1, weeks=1.0)
    assert balanced.total_capacity_hours == 40
    assert len(balanced.epics[0].tasks[0].subtasks) == 2
    subtasks = balanced.epics[0].tasks[0].subtasks
    assert sum(st.estimated_hours for st in subtasks) == balanced.epics[0].tasks[0].estimated_hours


@pytest.mark.asyncio
async def test_generate_project_plan_with_mock_router():
    """Test generate_project_plan using a mocked LLMRouter returning structured plan."""
    mock_router = MagicMock(spec=LLMRouter)
    mock_plan = ProjectPlan(
        project_name="AI Clinical Assistant",
        summary="Clinical document search assistant",
        developers_count=2,
        weeks_duration=2.0,
        total_capacity_hours=160,
        total_estimated_hours=160,
        epics=[
            EpicSpec(
                title="Data Ingestion",
                description="Parse EHR documents",
                estimated_hours=80,
                tasks=[
                    TaskSpec(
                        title="EHR Parser",
                        description="Parse HL7/FHIR",
                        estimated_hours=80,
                        subtasks=[
                            SubTaskSpec(title="FHIR Client", description="Connect to API", estimated_hours=40),
                            SubTaskSpec(title="Schema Validation", description="Validate records", estimated_hours=40),
                        ],
                    )
                ],
            ),
            EpicSpec(
                title="Chat UI",
                description="Frontend interface",
                estimated_hours=80,
                tasks=[
                    TaskSpec(
                        title="Chat Component",
                        description="Build React chat window",
                        estimated_hours=80,
                        subtasks=[
                            SubTaskSpec(title="Stream renderer", description="Render markdown", estimated_hours=40),
                            SubTaskSpec(title="Auth guard", description="Secure routes", estimated_hours=40),
                        ],
                    )
                ],
            ),
        ],
    )

    mock_router.route = AsyncMock(
        return_value=LLMResult(
            data=mock_plan,
            model_name="gemini-2.5-pro",
            provider="gemini",
            task_type="analysis",
            latency_ms=120.0,
        )
    )

    plan = await generate_project_plan(
        description="AI Clinical Assistant, 2 developers, 2 weeks",
        developers=2,
        weeks=2.0,
        router=mock_router,
    )

    assert plan.project_name == "AI Clinical Assistant"
    assert len(plan.epics) == 2
    assert plan.total_capacity_hours == 160


@pytest.mark.asyncio
async def test_generate_project_plan_fallback_on_error():
    """Test that planner falls back to deterministic rule-based plan if LLM fails."""
    mock_router = MagicMock(spec=LLMRouter)
    mock_router.route = AsyncMock(side_effect=RuntimeError("LLM quota exceeded or offline"))

    plan = await generate_project_plan(
        description="E-commerce Checkout Microservice",
        developers=2,
        weeks=1.0,
        router=mock_router,
    )

    assert plan.developers_count == 2
    assert plan.weeks_duration == 1.0
    assert plan.total_capacity_hours == 80
    assert len(plan.epics) == 4


@pytest.mark.asyncio
async def test_provision_jira_plan_dry_run():
    """Test that dry_run mode assigns simulated keys without calling Jira API."""
    plan = rule_based_fallback_plan("Telemetry Pipeline", developers=1, weeks=1.0)
    mock_jira_client = MagicMock()
    session_factory = get_session_factory()

    provisioned = await provision_jira_plan(
        plan=plan,
        project_key="KAN",
        jira_client=mock_jira_client,
        dry_run=True,
        session_factory=session_factory,
    )

    # JiraClient should not have been called
    mock_jira_client.create_issue.assert_not_called()

    # All epics, tasks, and subtasks must have DRY keys
    for epic in provisioned.epics:
        assert epic.jira_key.startswith("DRY-KAN-")
        for task in epic.tasks:
            assert task.jira_key.startswith("DRY-KAN-")
            for subtask in task.subtasks:
                assert subtask.jira_key.startswith("DRY-KAN-")

    # Verify audit entry in database
    with session_factory() as session:
        actions = session.query(ActionTaken).filter(ActionTaken.action_type == "jira_project_plan").all()
        assert len(actions) >= 1
        assert actions[-1].status == "dry_run"


@pytest.mark.asyncio
async def test_provision_jira_plan_live(mock_jira_issue_factory):
    """Test live Jira provisioning creates Epics, Tasks with parent Epic, and Subtasks with parent Task."""
    plan = ProjectPlan(
        project_name="Auth Service",
        summary="Authentication microservice",
        developers_count=1,
        weeks_duration=1.0,
        total_capacity_hours=40,
        total_estimated_hours=40,
        epics=[
            EpicSpec(
                title="Auth Core",
                description="Core auth services",
                estimated_hours=40,
                tasks=[
                    TaskSpec(
                        title="OAuth Provider",
                        description="Google & GitHub SSO",
                        estimated_hours=40,
                        subtasks=[
                            SubTaskSpec(title="Google Auth", description="Setup client", estimated_hours=20),
                            SubTaskSpec(title="JWT Signer", description="Tokens", estimated_hours=20),
                        ],
                    )
                ],
            )
        ],
    )

    mock_client = MagicMock()
    counter = [100]

    async def mock_create_issue(project_key, summary, description="", issue_type="Task", parent_key=None):
        key = f"KAN-{counter[0]}"
        counter[0] += 1
        return mock_jira_issue_factory(key=key, summary=summary)

    mock_client.create_issue = AsyncMock(side_effect=mock_create_issue)
    mock_client.close = AsyncMock()
    session_factory = get_session_factory()

    provisioned = await provision_jira_plan(
        plan=plan,
        project_key="KAN",
        jira_client=mock_client,
        dry_run=False,
        session_factory=session_factory,
    )

    assert mock_client.create_issue.call_count == 4  # 1 Epic + 1 Task + 2 Subtasks

    # Verify hierarchy linking
    epic_call = mock_client.create_issue.call_args_list[0]
    assert epic_call.kwargs["issue_type"] == "Epic"

    task_call = mock_client.create_issue.call_args_list[1]
    assert task_call.kwargs["issue_type"] == "Task"
    assert task_call.kwargs["parent_key"] == "KAN-100"

    subtask_call1 = mock_client.create_issue.call_args_list[2]
    assert subtask_call1.kwargs["issue_type"] == "Subtask"
    assert subtask_call1.kwargs["parent_key"] == "KAN-101"

    assert provisioned.epics[0].jira_key == "KAN-100"
    assert provisioned.epics[0].tasks[0].jira_key == "KAN-101"
    assert provisioned.epics[0].tasks[0].subtasks[0].jira_key == "KAN-102"
    assert provisioned.epics[0].tasks[0].subtasks[1].jira_key == "KAN-103"


def test_format_plan_for_slack():
    """Test format_plan_for_slack produces clean mrkdwn tree."""
    plan = rule_based_fallback_plan("Medical RAG", developers=2, weeks=2.0)
    for i, e in enumerate(plan.epics):
        e.jira_key = f"KAN-1{i}"
        for j, t in enumerate(e.tasks):
            t.jira_key = f"KAN-2{j}"
            for k, st in enumerate(t.subtasks):
                st.jira_key = f"KAN-3{k}"

    slack_text = format_plan_for_slack(plan, dry_run=False)
    assert ":dart: *AI Project Plan: Medical RAG*" in slack_text
    assert "2 developer(s) | *Duration:* 2.0 week(s)" in slack_text
    assert "• :package: *Epic:* `KAN-10`" in slack_text
    assert "└─ :memo: *Task:* `KAN-20`" in slack_text
    assert "└─ :hammer_and_wrench: `KAN-30`" in slack_text


def test_api_endpoint_project_plan_auth():
    """Test /project/plan endpoint requires authentication."""
    with TestClient(app) as client:
        # Without API key
        res = client.post("/project/plan", json={"description": "Medical RAG, 2 developers, 2 weeks"})
        assert res.status_code == 401

        # With invalid API key
        res = client.post(
            "/project/plan",
            json={"description": "Medical RAG, 2 developers, 2 weeks"},
            headers={"X-API-Key": "wrong-key"},
        )
        assert res.status_code == 401


def test_api_endpoint_project_plan_preview():
    """Test /project/plan/preview endpoint generates dry-run plan."""
    with TestClient(app) as client:
        res = client.post(
            "/project/plan/preview",
            json={"description": "Medical RAG, 2 developers, 2 weeks"},
            headers={"X-API-Key": "dummy-admin-key"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["developers_count"] == 2
        assert data["weeks_duration"] == 2.0
        assert data["total_capacity_hours"] == 160
        assert len(data["epics"]) >= 3
        # Check that dry-run keys were generated
        assert data["epics"][0]["jira_key"].startswith("DRY-")


def test_slack_command_plan():
    """Test /teampulse plan slash command dispatch and background execution."""
    from tests.test_slack_interaction import generate_slack_headers

    secret = "dummy-signing-secret-1234567890"
    body_str = "command=%2Fteampulse&text=plan+Medical+RAG%2C+2+developers%2C+2+weeks&user_id=U12345&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2F123"
    raw_body = body_str.encode("utf-8")
    headers = generate_slack_headers(raw_body, secret=secret)

    with TestClient(app) as client:
        with patch("app.api.routes.slack._post_command_response", new_callable=AsyncMock) as mock_post:
            res = client.post(
                "/slack/commands",
                content=raw_body,
                headers=headers,
            )
            assert res.status_code == 200
            data = res.json()
            assert "Generating AI Project Plan" in data["text"]
            assert mock_post.called
            sent_payload = mock_post.call_args[0][1]
            assert "AI Project Plan" in sent_payload["text"]


def test_ui_endpoints_serve_html_css_and_js():
    """Test frontend web UI routes serve HTML, CSS, JS and config."""
    with TestClient(app) as client:
        # 1. Test HTML endpoints
        for path in ["/", "/ui", "/planner"]:
            res = client.get(path)
            assert res.status_code == 200
            assert "text/html" in res.headers["content-type"]
            assert "TeamPulse" in res.text
            assert "Project Scope" in res.text

        # 2. Test CSS endpoint
        res_css = client.get("/static/styles.css")
        assert res_css.status_code == 200
        assert "text/css" in res_css.headers["content-type"]
        assert "--bg-canvas" in res_css.text

        # 3. Test JS endpoint
        res_js = client.get("/static/app.js")
        assert res_js.status_code == 200
        assert "text/javascript" in res_js.headers["content-type"]
        assert "loadTemplate" in res_js.text

        # 4. Test Config endpoint
        res_cfg = client.get("/project/config")
        assert res_cfg.status_code == 200
        cfg_data = res_cfg.json()
        assert "jira_project_key" in cfg_data
        assert "dry_run_default" in cfg_data


def test_update_ticket_endpoint_dry_run():
    """Test updating a ticket in dry-run/preview mode."""
    with TestClient(app) as client:
        res = client.post(
            "/project/ticket/update",
            json={
                "jira_key": "DRY-KAN-T1",
                "title": "Updated Task Title",
                "description": "Updated details",
                "estimated_hours": 12,
            },
            headers={"X-API-Key": "dummy-admin-key"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["title"] == "Updated Task Title"
        assert data["estimated_hours"] == 12


def test_update_ticket_endpoint_live():
    """Test updating a live Jira issue with mocked JiraClient."""
    with TestClient(app) as client:
        with patch("app.api.routes.planner.JiraClient.update_issue", new_callable=AsyncMock) as mock_update:
            mock_update.return_value = {}
            res = client.post(
                "/project/ticket/update",
                json={
                    "jira_key": "KAN-67",
                    "title": "Refined Database Schemas",
                    "description": "Added indexes",
                    "estimated_hours": 20,
                },
                headers={"X-API-Key": "dummy-admin-key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "KAN-67" in data["message"]
            assert mock_update.called
            call_kwargs = mock_update.call_args.kwargs
            assert call_kwargs["issue_key"] == "KAN-67"
            assert call_kwargs["summary"] == "Refined Database Schemas"
            assert call_kwargs["estimated_hours"] == 20



