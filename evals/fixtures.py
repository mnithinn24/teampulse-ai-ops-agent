"""Deterministic synthetic fixtures for offline evaluation suite."""

from collections import Counter
import random
from typing import Any, Dict, List, Optional
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue


SEED = 42
DEFAULT_REPO = "mnithinn24/teampulse-ai-ops-agent"
KNOWN_AUTHORS = ["alice", "bob", "carol", "dave", "eve", "frank"]


def generate_88_pr_dataset(repo: str = DEFAULT_REPO) -> List[PullRequest]:
    """Generate deterministic 88-PR dataset with ages from 20 to 3300 days."""
    rnd = random.Random(SEED)
    prs: List[PullRequest] = []

    # Ages linearly spaced from 20 to 3300 days
    # i=0 -> 20, i=87 -> 3300
    for i in range(88):
        num = i + 1
        age_days = int(round(20 + i * (3300 - 20) / 87))
        author = KNOWN_AUTHORS[i % len(KNOWN_AUTHORS)]
        
        # Some with reviewers, some without
        if i % 3 == 0:
            reviewers = [KNOWN_AUTHORS[(i + 1) % len(KNOWN_AUTHORS)]]
        elif i % 5 == 0:
            reviewers = [KNOWN_AUTHORS[(i + 1) % len(KNOWN_AUTHORS)], KNOWN_AUTHORS[(i + 2) % len(KNOWN_AUTHORS)]]
        else:
            reviewers = []

        # Last commit days between (age - 5) and age
        commit_offset = i % 6
        last_commit_days = max(10, age_days - commit_offset)
        is_stale = last_commit_days >= 7

        pr = PullRequest(
            id=1000 + num,
            number=num,
            title=f"Synthetic pull request #{num} addressing ops task",
            author=author,
            age_days=age_days,
            reviewers=reviewers,
            last_commit_days=last_commit_days,
            is_stale=is_stale,
            repo=repo,
            html_url=f"https://github.com/{repo}/pull/{num}",
        )
        prs.append(pr)

    return prs


def generate_3_jira_dataset() -> List[JiraIssue]:
    """Generate deterministic 3-issue Jira dataset (KAN-1 to KAN-3), all fresh."""
    return [
        JiraIssue(
            key="KAN-1",
            title="Setup database connection retry",
            status="Open",
            assignee="alice",
            age_days=3,
            days_since_update=1,
            comment_count=2,
            is_stale=False,
            project="KAN",
            issue_url="https://company.atlassian.net/browse/KAN-1",
        ),
        JiraIssue(
            key="KAN-2",
            title="Investigate memory spike in scheduler",
            status="In Progress",
            assignee="bob",
            age_days=4,
            days_since_update=2,
            comment_count=1,
            is_stale=False,
            project="KAN",
            issue_url="https://company.atlassian.net/browse/KAN-2",
        ),
        JiraIssue(
            key="KAN-3",
            title="Refactor Slack button callback security",
            status="Code Review",
            assignee="carol",
            age_days=2,
            days_since_update=1,
            comment_count=0,
            is_stale=False,
            project="KAN",
            issue_url="https://company.atlassian.net/browse/KAN-3",
        ),
    ]


def generate_stale_boundary_dataset(repo: str = DEFAULT_REPO) -> Dict[str, Any]:
    """Generate small boundary dataset with items right at, below, and above thresholds."""
    prs = [
        PullRequest(
            id=9001,
            number=91,
            title="Boundary PR: 6 days old",
            author="alice",
            age_days=6,
            last_commit_days=6,
            is_stale=False,  # Threshold is 7 days: 6 is fresh
            repo=repo,
            html_url=f"https://github.com/{repo}/pull/91",
        ),
        PullRequest(
            id=9002,
            number=92,
            title="Boundary PR: exactly 7 days old",
            author="bob",
            age_days=7,
            last_commit_days=7,
            is_stale=True,  # Threshold is 7 days: 7 is stale
            repo=repo,
            html_url=f"https://github.com/{repo}/pull/92",
        ),
        PullRequest(
            id=9003,
            number=93,
            title="Boundary PR: 8 days old",
            author="carol",
            age_days=8,
            last_commit_days=8,
            is_stale=True,  # Threshold is 7 days: 8 is stale
            repo=repo,
            html_url=f"https://github.com/{repo}/pull/93",
        ),
    ]

    tickets = [
        JiraIssue(
            key="KAN-13",
            title="Boundary ticket: 13 days inactive",
            status="Open",
            assignee="alice",
            age_days=20,
            days_since_update=13,
            is_stale=False,  # Threshold is 14 days: 13 is fresh
            project="KAN",
        ),
        JiraIssue(
            key="KAN-14",
            title="Boundary ticket: exactly 14 days inactive",
            status="In Progress",
            assignee="bob",
            age_days=20,
            days_since_update=14,
            is_stale=True,  # Threshold is 14 days: 14 is stale
            project="KAN",
        ),
        JiraIssue(
            key="KAN-15",
            title="Boundary ticket: 15 days inactive",
            status="Open",
            assignee="carol",
            age_days=20,
            days_since_update=15,
            is_stale=True,  # Threshold is 14 days: 15 is stale
            project="KAN",
        ),
    ]

    return {"prs": prs, "tickets": tickets}


# Independent computation functions for ground truth verification
def compute_ground_truth_totals(
    prs: List[PullRequest], tickets: List[JiraIssue]
) -> Dict[str, Any]:
    """Independently compute dataset totals without using application code."""
    open_prs = len(prs)
    stale_prs = sum(1 for p in prs if getattr(p, "is_stale", False))
    total_tickets = len(tickets)
    stale_tickets = sum(1 for t in tickets if getattr(t, "is_stale", False))
    author_counts = Counter(p.author for p in prs)

    return {
        "open_prs": open_prs,
        "stale_prs": stale_prs,
        "total_tickets": total_tickets,
        "stale_tickets": stale_tickets,
        "author_counts": dict(author_counts),
    }


compute_ground_truth_counts = compute_ground_truth_totals


def compute_ground_truth_rank(
    prs: List[PullRequest],
    metric: str = "age_days",
    limit: int = 10,
    order: str = "desc",
) -> List[Dict[str, Any]]:
    """Independently rank PRs by metric without using application code."""
    reverse = order.lower() == "desc"
    sorted_prs = sorted(
        prs,
        key=lambda p: getattr(p, metric, 0),
        reverse=reverse,
    )
    selected = sorted_prs[:limit]
    return [
        {
            "id": p.id,
            "number": p.number,
            "canonical_id": f"{p.repo}#{p.number}",
            "age_days": p.age_days,
            "last_commit_days": p.last_commit_days,
            "author": p.author,
        }
        for p in selected
    ]


def find_ground_truth_item(
    item_id: str,
    prs: List[PullRequest],
    tickets: List[JiraIssue],
) -> Optional[Dict[str, Any]]:
    """Independently lookup an item by key or canonical ID without application code."""
    needle = item_id.strip()
    for t in tickets:
        if t.key.lower() == needle.lower():
            return {
                "type": "jira_ticket",
                "key": t.key,
                "title": t.title,
                "status": t.status,
                "assignee": t.assignee,
                "age_days": t.age_days,
                "is_stale": t.is_stale,
            }

    for p in prs:
        canon = f"{p.repo}#{p.number}".lower()
        if (
            needle.lower() == canon
            or needle.lower() == f"#{p.number}"
            or needle == str(p.number)
            or needle == str(p.id)
        ):
            return {
                "type": "github_pr",
                "number": p.number,
                "canonical_id": f"{p.repo}#{p.number}",
                "title": p.title,
                "age_days": p.age_days,
                "author": p.author,
                "is_stale": p.is_stale,
            }

    return None
