"""CLI script to scan Jira project and display stale tickets."""

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Optional

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.config import get_settings, validate_jira_project_key
from app.integrations.jira_client import JiraClient, JiraIssue


def format_ticket_row(issue: JiraIssue) -> str:
    """Format a single JiraIssue model into a readable CLI display string.

    Args:
        issue: The JiraIssue model to format.

    Returns:
        Formatted summary string.
    """
    stale_tag = "[STALE]" if issue.is_stale else "[ACTIVE]"
    assignee_str = issue.assignee or "Unassigned"
    return (
        f"{stale_tag} [{issue.key}] {issue.title}\n"
        f"        Status: {issue.status} | Assignee: {assignee_str} | "
        f"Age: {issue.age_days}d | Last Update: {issue.days_since_update}d ago | "
        f"Comments: {issue.comment_count}\n"
        f"        URL: {issue.url}"
    )


async def run_check_tickets(
    project_key: Optional[str] = None,
    stale_days: Optional[int] = None,
    show_all: bool = False,
) -> int:
    """Execute stale ticket check for the target Jira project.

    Args:
        project_key: Optional explicit Jira project key.
        stale_days: Optional custom threshold for staleness in days.
        show_all: If True, print active as well as stale tickets.

    Returns:
        Exit code (0 if success, 1 on error).
    """
    try:
        settings = get_settings()
    except Exception as conf_err:
        print(f"Configuration Error: {conf_err}", file=sys.stderr)
        return 1

    target_project = settings.jira_project_key if project_key is None else project_key

    if not target_project:
        print(
            "Error: No Jira project key specified. Set JIRA_PROJECT_KEY in .env "
            "or pass it via --project PROJ.",
            file=sys.stderr,
        )
        return 1

    try:
        validated_project = validate_jira_project_key(target_project)
    except ValueError as val_err:
        print(f"Error: {val_err}", file=sys.stderr)
        return 1

    if not settings.jira_base_url:
        print(
            "Warning: JIRA_BASE_URL is not configured in .env. API calls will fail.",
            file=sys.stderr,
        )

    threshold = stale_days if stale_days is not None else settings.stale_ticket_days
    print(f"--- TeamPulse: Scanning Jira for Stale Tickets ---")
    print(f"Target Project: {validated_project}")
    print(f"Stale Threshold: {threshold} days\n")

    async with JiraClient(project_key=validated_project, stale_ticket_days=threshold) as client:
        try:
            issues = await client.fetch_open_issues(project_key=validated_project)
        except Exception as exc:
            print(f"Error fetching Jira tickets: {exc}", file=sys.stderr)
            return 1

    if not issues:
        print(f"No open Jira tickets found for project '{validated_project}'.")
        return 0

    stale_issues = [i for i in issues if i.is_stale]
    active_issues = [i for i in issues if not i.is_stale]

    print(
        f"Found {len(issues)} open tickets ({len(stale_issues)} stale, "
        f"{len(active_issues)} active).\n"
    )

    if stale_issues:
        print("=== Stale Jira Tickets ===")
        for issue in stale_issues:
            print(format_ticket_row(issue))
            print()
    else:
        print("Good news! No stale Jira tickets found.")

    if show_all and active_issues:
        print("\n=== Active Jira Tickets ===")
        for issue in active_issues:
            print(format_ticket_row(issue))
            print()

    return 0


def main() -> None:
    """Parse command line arguments and execute Jira ticket inspection."""
    parser = argparse.ArgumentParser(
        description="Scan Jira for unresolved tickets with no recent updates."
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Jira project key (defaults to JIRA_PROJECT_KEY in .env).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Days without updates to consider a ticket stale (defaults to STALE_TICKET_DAYS).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show all open tickets, not just stale ones.",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(
        run_check_tickets(
            project_key=args.project,
            stale_days=args.days,
            show_all=args.show_all,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
