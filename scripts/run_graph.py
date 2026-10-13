"""CLI runner to execute the LangGraph pipeline and display final state."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.config import get_settings
from app.db.database import get_db_session, init_db
from app.db.models import AgentRun
from app.graph.pipeline import create_pipeline
from app.graph.state import TeamPulseState


async def execute_graph(
    workspace_id: str = "default",
    run_type: str = "ondemand",
    query: str = "",
) -> int:
    """Initialize database, run LangGraph pipeline, log run, and display state.

    Args:
        workspace_id: Target workspace identifier.
        run_type: Type of run (morning, evening, ondemand).
        query: Optional custom on-demand query.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    try:
        settings = get_settings()
    except Exception as conf_err:
        print(f"Configuration Error: {conf_err}", file=sys.stderr)
        return 1

    print("=== TeamPulse LangGraph Pipeline Runner ===")
    print(f"Workspace          : {workspace_id} | Run Type: {run_type}")
    print(f"Configured Repos   : {', '.join(settings.github_repos) if settings.github_repos else 'None'}")
    print(f"Target Jira Project: {settings.jira_project_key or 'None'}")
    if query:
        print(f"Focus Query        : {query}")
    print()

    # Step 1: Ensure database tables exist
    try:
        init_db()
        print("[Database] Schema verified and ready.")
    except Exception as db_err:
        print(f"[Database] Warning: Could not initialize DB: {db_err}")

    # Step 2: Initialize pipeline and state
    pipeline = create_pipeline()
    initial_state = TeamPulseState(
        workspace_id=workspace_id,
        run_type=run_type,
        query=query or None,
    )

    print("[Pipeline] Running: START -> Collect -> Analyze -> Act -> END ...\n")
    try:
        final_state = await pipeline.ainvoke(initial_state)
    except Exception as pipe_err:
        print(f"Pipeline execution error: {pipe_err}", file=sys.stderr)
        return 1

    # Convert state to dictionary if returned as model or dict
    state_dict = final_state if isinstance(final_state, dict) else final_state.model_dump()

    # Step 3: Print final state components
    print("================ FINAL PIPELINE STATE ================")
    print(f"Workspace ID : {state_dict.get('workspace_id')}")
    print(f"Run Type     : {state_dict.get('run_type')}")
    print(f"Query        : {state_dict.get('query')}")

    # GitHub Data Summary
    gh_data = state_dict.get("github_data")
    if isinstance(gh_data, list):
        print(f"\n[GitHub Data] : {len(gh_data)} open PR(s) collected")
        for pr in gh_data[:5]:
            pr_title = getattr(pr, "title", None) or (pr.get("title") if isinstance(pr, dict) else str(pr))
            pr_stale = getattr(pr, "is_stale", None) or (pr.get("is_stale") if isinstance(pr, dict) else False)
            tag = "[STALE]" if pr_stale else "[ACTIVE]"
            print(f"  * {tag} {pr_title}")
        if len(gh_data) > 5:
            print(f"  ... and {len(gh_data) - 5} more")
    else:
        print(f"\n[GitHub Data] : {gh_data}")

    # Jira Data Summary
    jr_data = state_dict.get("jira_data")
    if isinstance(jr_data, list):
        print(f"\n[Jira Data]   : {len(jr_data)} open ticket(s) collected")
        for issue in jr_data[:5]:
            key = getattr(issue, "key", None) or (issue.get("key") if isinstance(issue, dict) else str(issue))
            title = getattr(issue, "title", None) or (issue.get("title") if isinstance(issue, dict) else "")
            stale = getattr(issue, "is_stale", None) or (issue.get("is_stale") if isinstance(issue, dict) else False)
            tag = "[STALE]" if stale else "[ACTIVE]"
            print(f"  * {tag} [{key}] {title}")
        if len(jr_data) > 5:
            print(f"  ... and {len(jr_data) - 5} more")
    else:
        print(f"\n[Jira Data]   : {jr_data}")

    # Analysis
    analysis = state_dict.get("analysis")
    print(f"\n[Analysis]    : {json.dumps(analysis, indent=2) if isinstance(analysis, dict) else analysis}")

    # Priority Items
    priority_items = state_dict.get("priority_items")
    print(f"\n[Priority Items]: {json.dumps(priority_items, indent=2)}")

    # Briefing
    briefing = state_dict.get("briefing")
    print(f"\n[Briefing]    : {json.dumps(briefing, indent=2) if isinstance(briefing, dict) else briefing}")

    # Errors
    errors = state_dict.get("errors", [])
    if errors:
        print(f"\n[Non-Fatal Errors Encountered ({len(errors)})]:")
        for err in errors:
            print(f"  ! {err}")

    print("======================================================")

    # Step 4: Record run in database
    try:
        with get_db_session() as session:
            run_record = AgentRun(
                workspace_id=workspace_id,
                run_type=run_type,
                status="completed",
                query=query or None,
                analysis=json.dumps(analysis) if isinstance(analysis, dict) else str(analysis),
                briefing=json.dumps(briefing) if isinstance(briefing, dict) else str(briefing),
                errors=json.dumps(errors) if errors else None,
            )
            session.add(run_record)
        print(f"[Database] Agent run recorded successfully.")
    except Exception as save_err:
        print(f"[Database] Notice: Could not save run to DB: {save_err}")

    return 0


def main() -> None:
    """Parse arguments and run graph execution."""
    parser = argparse.ArgumentParser(
        description="Run TeamPulse LangGraph pipeline and print state."
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default="default",
        help="Workspace ID (defaults to 'default').",
    )
    parser.add_argument(
        "--type",
        type=str,
        default="ondemand",
        choices=["morning", "evening", "ondemand"],
        help="Run type (morning, evening, ondemand).",
    )
    parser.add_argument(
        "--query",
        type=str,
        default="",
        help="Optional custom query for this run.",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(
        execute_graph(
            workspace_id=args.workspace,
            run_type=args.type,
            query=args.query,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
