"""Pipeline execution runner with concurrency guard and metric tracking."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import select

from app.config import get_settings
from app.core.errors import PipelineConcurrencyError
from app.db.database import get_db_session
from app.db.models import AgentRun, utc_now
from app.graph.pipeline import run_act, run_analyze, run_collect
from app.graph.state import TeamPulseState
from app.integrations.github_client import GitHubClient
from app.integrations.jira_client import JiraClient
from app.integrations.slack_client import SlackClient
from app.llm.router import LLMRouter

logger = logging.getLogger(__name__)

# In-memory concurrency tracking per workspace
_active_workspaces: Set[str] = set()
_guard_lock = asyncio.Lock()


def is_workspace_running(workspace_id: str) -> bool:
    """Check if a pipeline run is currently in progress for the workspace.

    Evaluates both in-memory active sets and database runs with status 'running'.

    Args:
        workspace_id: Unique team workspace identifier.

    Returns:
        True if an active run is detected, False otherwise.
    """
    if workspace_id in _active_workspaces:
        return True

    with get_db_session() as session:
        running_run = session.scalars(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.status == "running",
            )
        ).first()
        return running_run is not None


def mark_workspace_completed(workspace_id: str) -> None:
    """Helper to remove workspace from active tracking set (e.g. for testing cleanup)."""
    _active_workspaces.discard(workspace_id)


async def execute_pipeline_task(
    run_id: int,
    workspace_id: str,
    run_type: str = "ondemand",
    query: Optional[str] = None,
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
    router: Optional[LLMRouter] = None,
    slack_client: Optional[SlackClient] = None,
) -> TeamPulseState:
    """Execute all three pipeline nodes sequentially with fine-grained performance tracking.

    Updates the existing AgentRun row with per-node latencies, model metadata, items count,
    and completion status.

    Args:
        run_id: Database AgentRun primary key ID.
        workspace_id: Target workspace identifier.
        run_type: 'morning', 'evening', or 'ondemand'.
        query: Optional on-demand focus query.
        github_client: Optional custom/mocked GitHub client.
        jira_client: Optional custom/mocked Jira client.
        router: Optional custom/mocked LLM router.
        slack_client: Optional custom/mocked Slack client.

    Returns:
        The final TeamPulseState.
    """
    settings = get_settings()
    logger.info("Starting pipeline execution for run_id=%s, workspace=%s", run_id, workspace_id)

    state = TeamPulseState(
        workspace_id=workspace_id,
        run_type=run_type,
        query=query,
        run_id=run_id,
    )

    t_total_start = time.perf_counter()
    collect_ms = 0.0
    analyze_ms = 0.0
    act_ms = 0.0

    try:
        # 1. Collect Node
        t_col_start = time.perf_counter()
        col_res = await run_collect(state, github_client=github_client, jira_client=jira_client)
        collect_ms = (time.perf_counter() - t_col_start) * 1000
        state.github_data = col_res["github_data"]
        state.jira_data = col_res["jira_data"]
        state.errors = col_res["errors"]

        # Count collected items
        prs_count = len(state.github_data) if isinstance(state.github_data, list) else 0
        jira_count = len(state.jira_data) if isinstance(state.jira_data, list) else 0
        items_collected = prs_count + jira_count

        # 2. Analyze Node
        t_ana_start = time.perf_counter()
        ana_res = await run_analyze(state, router=router)
        analyze_ms = (time.perf_counter() - t_ana_start) * 1000
        state.analysis = ana_res["analysis"]
        state.priority_items = ana_res["priority_items"]
        state.errors = ana_res["errors"]

        meta = state.analysis.get("_meta", {}) if isinstance(state.analysis, dict) else {}
        model_used = meta.get("model_name", "unknown")
        fallback_used = bool(
            meta.get("fallback_used", False)
            or (settings.gemini_model and model_used != settings.gemini_model)
        )

        # 3. Act Node
        t_act_start = time.perf_counter()
        act_res = await run_act(state, slack_client=slack_client)
        act_ms = (time.perf_counter() - t_act_start) * 1000
        state.briefing = act_res.get("briefing")
        state.errors = act_res.get("errors", state.errors)

        total_latency_ms = (time.perf_counter() - t_total_start) * 1000

        briefing_dict = state.briefing if isinstance(state.briefing, dict) else {}
        flags_created = briefing_dict.get("flags_saved", len(state.priority_items))

        # Record metrics in AgentRun
        with get_db_session() as session:
            run = session.get(AgentRun, run_id)
            if run is not None:
                run.status = "completed"
                run.completed_at = utc_now()
                run.total_latency_ms = round(total_latency_ms, 2)
                run.collect_latency_ms = round(collect_ms, 2)
                run.analyze_latency_ms = round(analyze_ms, 2)
                run.act_latency_ms = round(act_ms, 2)
                run.model_used = model_used
                run.fallback_used = fallback_used
                run.items_collected = items_collected
                run.flags_created = flags_created
                run.errors = json.dumps(state.errors) if state.errors else None
                session.commit()

        logger.info(
            "Pipeline run_id=%s completed in %.2fms (collect=%.2f, analyze=%.2f, act=%.2f)",
            run_id,
            total_latency_ms,
            collect_ms,
            analyze_ms,
            act_ms,
        )
        return state

    except Exception as exc:
        total_latency_ms = (time.perf_counter() - t_total_start) * 1000
        logger.error("Pipeline run_id=%s failed with error: %s", run_id, exc)
        with get_db_session() as session:
            run = session.get(AgentRun, run_id)
            if run is not None:
                run.status = "failed"
                run.completed_at = utc_now()
                run.total_latency_ms = round(total_latency_ms, 2)
                run.collect_latency_ms = round(collect_ms, 2)
                run.analyze_latency_ms = round(analyze_ms, 2)
                run.act_latency_ms = round(act_ms, 2)
                errs = list(state.errors) + [f"Unhandled pipeline error: {exc}"]
                run.errors = json.dumps(errs)
                session.commit()
        raise

    finally:
        _active_workspaces.discard(workspace_id)


async def trigger_pipeline_run(
    workspace_id: str = "default",
    run_type: str = "ondemand",
    query: Optional[str] = None,
    background: bool = True,
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
    router: Optional[LLMRouter] = None,
    slack_client: Optional[SlackClient] = None,
) -> Tuple[int, Optional[TeamPulseState]]:
    """Trigger a new pipeline run guarded by concurrency locks.

    Raises:
        PipelineConcurrencyError: If another run is already in progress for this workspace.

    Returns:
        Tuple of (run_id, optional state if executed synchronously).
    """
    async with _guard_lock:
        if is_workspace_running(workspace_id):
            logger.warning("Concurrency guard blocked duplicate run for workspace '%s'", workspace_id)
            raise PipelineConcurrencyError(
                f"A pipeline run is already in progress for workspace '{workspace_id}'."
            )

        # Mark workspace as actively executing
        _active_workspaces.add(workspace_id)

        # Create AgentRun with status 'running'
        with get_db_session() as session:
            agent_run = AgentRun(
                workspace_id=workspace_id,
                run_type=run_type,
                status="running",
                query=query,
                created_at=utc_now(),
            )
            session.add(agent_run)
            session.commit()
            session.refresh(agent_run)
            run_id = agent_run.id

    # Execute in background or synchronously
    if background:
        asyncio.create_task(
            execute_pipeline_task(
                run_id=run_id,
                workspace_id=workspace_id,
                run_type=run_type,
                query=query,
                github_client=github_client,
                jira_client=jira_client,
                router=router,
                slack_client=slack_client,
            )
        )
        return run_id, None
    else:
        final_state = await execute_pipeline_task(
            run_id=run_id,
            workspace_id=workspace_id,
            run_type=run_type,
            query=query,
            github_client=github_client,
            jira_client=jira_client,
            router=router,
            slack_client=slack_client,
        )
        return run_id, final_state
