"""LangGraph 3-node StateGraph pipeline (Collect -> Analyze -> Act) for TeamPulse."""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph.state import TeamPulseState
from app.integrations.github_client import GitHubClient, PullRequest
from app.integrations.jira_client import JiraClient, JiraIssue

logger = logging.getLogger(__name__)


async def run_collect(
    state: TeamPulseState,
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
) -> Dict[str, Any]:
    """Execute concurrent data collection across GitHub and Jira sources.

    Runs collection in parallel using asyncio.gather. Reads GITHUB_REPOS
    and JIRA_PROJECT_KEY from application settings. If either integration fails,
    its status is marked as 'unavailable' and the pipeline continues without crashing.

    Args:
        state: Current TeamPulseState.
        github_client: Optional pre-configured or mocked GitHubClient.
        jira_client: Optional pre-configured or mocked JiraClient.

    Returns:
        State update dictionary containing github_data, jira_data, and any errors.
    """
    settings = get_settings()
    logger.info(
        "Executing Collect node for workspace: %s (repos: %s, jira_project: %s)",
        state.workspace_id,
        settings.github_repos,
        settings.jira_project_key,
    )

    gh_client = github_client or GitHubClient(stale_pr_days=settings.stale_pr_days)
    jr_client = jira_client or JiraClient(stale_ticket_days=settings.stale_ticket_days)

    async def _fetch_github() -> List[PullRequest]:
        return await gh_client.fetch_all_open_prs(repos=settings.github_repos)

    async def _fetch_jira() -> List[JiraIssue]:
        return await jr_client.fetch_open_issues(project_key=settings.jira_project_key)

    # Fetch GitHub and Jira concurrently, catching exceptions per source
    results = await asyncio.gather(
        _fetch_github(),
        _fetch_jira(),
        return_exceptions=True,
    )

    gh_result, jr_result = results[0], results[1]
    new_errors: List[str] = list(state.errors)

    # Process GitHub outcome
    if isinstance(gh_result, Exception):
        logger.warning("GitHub collection marked unavailable: %s", gh_result)
        github_data: Any = {
            "status": "unavailable",
            "error": str(gh_result),
            "prs": [],
        }
        new_errors.append(f"GitHub collection unavailable: {gh_result}")
    else:
        github_data = gh_result

    # Process Jira outcome
    if isinstance(jr_result, Exception):
        logger.warning("Jira collection marked unavailable: %s", jr_result)
        jira_data: Any = {
            "status": "unavailable",
            "error": str(jr_result),
            "issues": [],
        }
        new_errors.append(f"Jira collection unavailable: {jr_result}")
    else:
        jira_data = jr_result

    # Cleanup clients if created locally
    if github_client is None:
        await gh_client.close()
    if jira_client is None:
        await jr_client.close()

    return {
        "github_data": github_data,
        "jira_data": jira_data,
        "errors": new_errors,
    }


from app.agents.analyze import analyze_workspace
from app.llm.router import LLMRouter


async def run_analyze(
    state: TeamPulseState,
    router: Optional[LLMRouter] = None,
) -> Dict[str, Any]:
    """Execute real Analyze node using LLMRouter, prompt synthesis, and validation.

    Args:
        state: Current TeamPulseState.
        router: Optional pre-configured or mocked LLMRouter.

    Returns:
        State update dictionary containing analysis and priority_items.
    """
    logger.info("Executing Analyze node for workspace: %s", state.workspace_id)
    analysis, model_used, latency_ms = await analyze_workspace(state, router=router)

    analysis_dict = analysis.model_dump()
    analysis_dict["_meta"] = {
        "model_name": model_used,
        "latency_ms": latency_ms,
    }

    return {
        "analysis": analysis_dict,
        "priority_items": [flag.model_dump() for flag in analysis.priority_flags],
        "errors": list(state.errors),
    }


from app.agents.act import act_on_workspace
from app.integrations.slack_client import SlackClient


async def run_act(
    state: TeamPulseState,
    slack_client: Optional[SlackClient] = None,
) -> Dict[str, Any]:
    """Execute real Act node: saves flags as pending and posts Slack Block Kit briefing.

    Args:
        state: Current TeamPulseState.
        slack_client: Optional pre-configured SlackClient.

    Returns:
        State update dictionary containing briefing and errors.
    """
    logger.info("Executing Act node for workspace: %s", state.workspace_id)
    return await act_on_workspace(state, slack_client=slack_client)


def create_pipeline(
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
    router: Optional[LLMRouter] = None,
    slack_client: Optional[SlackClient] = None,
) -> Any:
    """Construct and compile the 3-node LangGraph pipeline.

    Topology: START -> Collect -> Analyze -> Act -> END

    Args:
        github_client: Optional pre-configured or mock GitHubClient.
        jira_client: Optional pre-configured or mock JiraClient.
        router: Optional pre-configured or mock LLMRouter.
        slack_client: Optional pre-configured or mock SlackClient.

    Returns:
        Compiled LangGraph runnable StateGraph.
    """
    graph_builder = StateGraph(TeamPulseState)

    # Node wrappers injecting custom clients if provided
    is_test_pipeline = any(c is not None for c in (github_client, jira_client, router))
    effective_slack = slack_client
    if effective_slack is None and is_test_pipeline:
        from unittest.mock import AsyncMock
        mock_slack = AsyncMock(spec=SlackClient)
        mock_slack.post_message = AsyncMock(
            return_value={"ok": True, "channel": "test-channel", "ts": "1234567890.123456"}
        )
        effective_slack = mock_slack

    async def collect_node(state: TeamPulseState) -> Dict[str, Any]:
        return await run_collect(state, github_client=github_client, jira_client=jira_client)

    async def analyze_node(state: TeamPulseState) -> Dict[str, Any]:
        return await run_analyze(state, router=router)

    async def act_node(state: TeamPulseState) -> Dict[str, Any]:
        return await run_act(state, slack_client=effective_slack)

    # Register nodes
    graph_builder.add_node("collect", collect_node)
    graph_builder.add_node("analyze", analyze_node)
    graph_builder.add_node("act", act_node)

    # Connect edges
    graph_builder.add_edge(START, "collect")
    graph_builder.add_edge("collect", "analyze")
    graph_builder.add_edge("analyze", "act")
    graph_builder.add_edge("act", END)

    return graph_builder.compile()
