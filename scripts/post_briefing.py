"""CLI script to execute the TeamPulse pipeline and post the Block Kit briefing to Slack."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.config import get_settings
from app.db.database import init_db
from app.graph.pipeline import create_pipeline
from app.graph.state import TeamPulseState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("teampulse.post_briefing")


async def main() -> int:
    """Run TeamPulse pipeline and post briefing to Slack."""
    parser = argparse.ArgumentParser(
        description="Run TeamPulse pipeline and post briefing to Slack."
    )
    parser.add_argument(
        "--workspace",
        default="default",
        help="Workspace identifier (default: 'default')",
    )
    parser.add_argument(
        "--run-type",
        default="ondemand",
        choices=["morning", "evening", "ondemand"],
        help="Run type for agent run tracking (default: 'ondemand')",
    )
    args = parser.parse_args()

    settings = get_settings()
    print("=" * 60)
    print(" TeamPulse Ops Briefing Runner (Phase 4)")
    print("=" * 60)
    print(f"Workspace: {args.workspace}")
    print(f"Run Type : {args.run_type}")
    print(f"Repos    : {settings.github_repos}")
    print(f"Jira Proj: {settings.jira_project_key}")
    print(f"Channel  : {settings.slack_channel_id or '(Not configured)'}")
    print("=" * 60)

    # 1. Initialize database tables
    print("[1/3] Initializing database...")
    init_db()

    # 2. Build and execute pipeline
    print("[2/3] Executing LangGraph pipeline (Collect -> Analyze -> Act)...")
    pipeline = create_pipeline()
    initial_state = TeamPulseState(
        workspace_id=args.workspace,
        run_type=args.run_type,
    )

    result = await pipeline.ainvoke(initial_state)
    state_dict = result if isinstance(result, dict) else result.model_dump()

    # 3. Display outcome
    briefing = state_dict.get("briefing") or {}
    slack_channel = briefing.get("slack_channel")
    slack_ts = briefing.get("slack_ts")
    flags_saved = briefing.get("flags_saved", 0)

    print("\n" + "=" * 60)
    print(" Pipeline Execution Complete")
    print("=" * 60)
    print(f"Flags Saved to DB: {flags_saved}")
    if slack_channel and slack_ts:
        print(f"Slack Status     : Posted to channel {slack_channel} (ts: {slack_ts})")
    else:
        print("Slack Status     : Briefing generated (Slack posting skipped or unconfigured)")

    print("\n[Briefing Preview]:")
    print(briefing.get("text", "No briefing text generated."))

    errors = state_dict.get("errors") or []
    if errors:
        print(f"\n[Warnings / Errors ({len(errors)})]:")
        for err in errors:
            print(f"  - {err}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
