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


async def run_analyze(state: TeamPulseState) -> Dict[str, Any]:
    """Analyze node stub for Phase 2.

    In Phase 3, this will invoke the LLM router (Ollama/Gemini) to evaluate
    patterns, deduplicate against previously flagged items, and prioritize issues.

    Args:
        state: Current TeamPulseState.

    Returns:
        State update dictionary with placeholder analysis and priority items.
    """
    logger.info("Executing Analyze node (Phase 2 stub)")

    # Count collected items if available
    gh_count = len(state.github_data) if isinstance(state.github_data, list) else 0
    jr_count = len(state.jira_data) if isinstance(state.jira_data, list) else 0

    analysis_stub = {
        "summary": f"Phase 2 stub analysis. Found {gh_count} PRs and {jr_count} Jira issues.",
        "findings": ["Analysis logic will be implemented in Phase 3."],
    }

    priority_items_stub = [
        {
            "id": "sample-priority-1",
            "source": "stub",
            "title": "Sample priority item for Phase 2 pipeline verification",
            "urgency": "medium",
        }
    ]

    return {
        "analysis": analysis_stub,
        "priority_items": priority_items_stub,
    }


async def run_act(state: TeamPulseState) -> Dict[str, Any]:
    """Act node stub for Phase 2.

    In Phase 4, this will construct the Slack Block Kit briefing and prepare
    approval buttons for human-in-the-loop actions.

    Args:
        state: Current TeamPulseState.

    Returns:
        State update dictionary with placeholder briefing.
    """
    logger.info("Executing Act node (Phase 2 stub)")

    briefing_stub = {
        "text": f"TeamPulse Briefing for workspace '{state.workspace_id}' (Phase 2 stub)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "TeamPulse Ops Briefing (Stub)"},
            }
        ],
        "status": "ready_for_review",
    }

    return {
        "briefing": briefing_stub,
    }


def create_pipeline(
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
) -> Any:
    """Construct and compile the 3-node LangGraph pipeline.

    Topology: START -> Collect -> Analyze -> Act -> END

    Args:
        github_client: Optional pre-configured or mock GitHubClient.
        jira_client: Optional pre-configured or mock JiraClient.

    Returns:
        Compiled LangGraph runnable StateGraph.
    """
    graph_builder = StateGraph(TeamPulseState)

    # Node wrappers injecting custom clients if provided
    async def collect_node(state: TeamPulseState) -> Dict[str, Any]:
        return await run_collect(state, github_client=github_client, jira_client=jira_client)

    async def analyze_node(state: TeamPulseState) -> Dict[str, Any]:
        return await run_analyze(state)

    async def act_node(state: TeamPulseState) -> Dict[str, Any]:
        return await run_act(state)

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
