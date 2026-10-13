"""CLI script to scan configured repositories and display stale pull requests."""

import argparse
import asyncio
from pathlib import Path
import sys
from typing import List, Optional

# Ensure project root is in sys.path for direct script execution
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.config import get_settings
from app.integrations.github_client import GitHubClient, PullRequest


def format_pr_row(pr: PullRequest) -> str:
    """Format a single PullRequest into a readable CLI summary block.

    Args:
        pr: The PullRequest model to format.

    Returns:
        Formatted string representation.
    """
    reviewers_str = ", ".join(pr.reviewers) if pr.reviewers else "None assigned"
    stale_tag = "[STALE]" if pr.is_stale else "[ACTIVE]"
    return (
        f"{stale_tag} {pr.repo} #{pr.number or pr.id}: {pr.title}\n"
        f"        Author: {pr.author} | Age: {pr.age_days}d | "
        f"Last Commit: {pr.last_commit_days}d ago | Reviewers: {reviewers_str}\n"
        f"        URL: {pr.url}"
    )


async def run_check(
    repos: Optional[List[str]] = None,
    stale_days: Optional[int] = None,
    show_all: bool = False,
) -> int:
    """Execute stale PR check across target repositories.

    Args:
        repos: Optional explicit list of repositories (owner/repo).
        stale_days: Optional custom threshold for staleness in days.
        show_all: If True, list both active and stale PRs.

    Returns:
        Exit code (0 if completed, 1 on error).
    """
    settings = get_settings()
    target_repos = settings.github_repos if repos is None else repos

    if not target_repos:
        print(
            "No repositories configured! Please specify GITHUB_REPOS in your .env file "
            "or pass repos as arguments (e.g. python scripts/check_stale_prs.py owner/repo).",
            file=sys.stderr,
        )
        return 1

    threshold = stale_days if stale_days is not None else settings.stale_pr_days
    print(f"--- TeamPulse: Scanning for Pull Requests ---")
    print(f"Target Repositories: {', '.join(target_repos)}")
    print(f"Stale Threshold: {threshold} days\n")

    async with GitHubClient(stale_pr_days=threshold) as client:
        try:
            prs = await client.fetch_all_open_prs(repos=target_repos)
        except Exception as exc:
            print(f"Error fetching PRs: {exc}", file=sys.stderr)
            return 1

    if not prs:
        print("No open pull requests found across target repositories.")
        return 0

    stale_prs = [pr for pr in prs if pr.is_stale]
    active_prs = [pr for pr in prs if not pr.is_stale]

    print(f"Found {len(prs)} open PRs total ({len(stale_prs)} stale, {len(active_prs)} active).\n")

    if stale_prs:
        print("=== Stale Pull Requests ===")
        for pr in stale_prs:
            print(format_pr_row(pr))
            print()
    else:
        print("Good news! No stale pull requests detected.")

    if show_all and active_prs:
        print("\n=== Active Pull Requests ===")
        for pr in active_prs:
            print(format_pr_row(pr))
            print()

    return 0


def main() -> None:
    """Parse CLI arguments and run the async stale PR checker."""
    parser = argparse.ArgumentParser(
        description="Scan GitHub repositories for stale pull requests."
    )
    parser.add_argument(
        "repos",
        nargs="*",
        help="Optional list of repositories to inspect (owner/repo). Defaults to GITHUB_REPOS env.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Days threshold to classify a PR as stale (defaults to STALE_PR_DAYS).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show all open pull requests, not just stale ones.",
    )

    args = parser.parse_args()
    repos = args.repos if args.repos else None
    exit_code = asyncio.run(
        run_check(repos=repos, stale_days=args.days, show_all=args.show_all)
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
