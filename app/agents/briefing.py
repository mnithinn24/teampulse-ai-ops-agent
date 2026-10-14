"""Slack Block Kit briefing builder from analysis results and database flags."""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from app.config import get_settings
from app.core.constants import URGENCY_CRITICAL, URGENCY_URGENT, URGENCY_WATCH
from app.graph.state import TeamPulseState
from app.llm.schemas import Analysis, PriorityFlag

logger = logging.getLogger(__name__)

SLACK_MAX_BLOCKS = 50
SLACK_TEXT_MAX_LENGTH = 3000


def format_item_link(item_id: str, jira_base_url: Optional[str] = None) -> str:
    """Format canonical item ID into a clickable Slack markdown link if possible.

    Args:
        item_id: Canonical item ID (e.g. 'owner/repo#123' or 'KAN-42').
        jira_base_url: Optional base Jira Cloud URL.

    Returns:
        Slack formatted link string like '<url|label>' or plain label.
    """
    clean_id = item_id.strip()

    # GitHub PR format: owner/repo#number
    gh_match = re.match(r"^([^/]+/[^#]+)#(\d+)$", clean_id)
    if gh_match:
        repo_slug, pr_num = gh_match.groups()
        url = f"https://github.com/{repo_slug}/pull/{pr_num}"
        return f"<{url}|{clean_id}>"

    # Jira Key format: PROJECT-123
    jira_match = re.match(r"^[A-Z][A-Z0-9_]+-\d+$", clean_id)
    if jira_match:
        base_url = (jira_base_url or get_settings().jira_base_url or "").rstrip("/")
        if base_url:
            url = f"{base_url}/browse/{clean_id}"
            return f"<{url}|{clean_id}>"

    return clean_id


def extract_totals(state: TeamPulseState) -> Tuple[int, int, int]:
    """Calculate counts for open PRs, stale PRs, and stale Jira tickets.

    Args:
        state: Pipeline TeamPulseState.

    Returns:
        Tuple of (open_prs_count, stale_prs_count, stale_tickets_count).
    """
    open_prs = 0
    stale_prs = 0
    stale_tickets = 0

    # Parse GitHub data
    gh_data = state.github_data
    prs_list: List[Any] = []
    if isinstance(gh_data, list):
        prs_list = gh_data
    elif isinstance(gh_data, dict):
        prs_list = gh_data.get("prs", [])

    for pr in prs_list:
        open_prs += 1
        is_stale = getattr(pr, "is_stale", False) if not isinstance(pr, dict) else pr.get("is_stale", False)
        if is_stale:
            stale_prs += 1

    # Parse Jira data
    jr_data = state.jira_data
    issues_list: List[Any] = []
    if isinstance(jr_data, list):
        issues_list = jr_data
    elif isinstance(jr_data, dict):
        issues_list = jr_data.get("issues", [])

    for issue in issues_list:
        is_stale = getattr(issue, "is_stale", False) if not isinstance(issue, dict) else issue.get("is_stale", False)
        if is_stale:
            stale_tickets += 1

    return open_prs, stale_prs, stale_tickets


def truncate_slack_text(text: str, max_chars: int = 2800) -> str:
    """Safely truncate text to avoid exceeding Slack 3000 character block limit.

    Args:
        text: Input text string.
        max_chars: Maximum characters allowed before truncation.

    Returns:
        Truncated text ending with an ellipsis if shortened.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def build_slack_briefing(
    state: TeamPulseState,
    flags_with_db: List[Dict[str, Any]],
    max_flags: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Construct Slack Block Kit layout and plain-text fallback for TeamPulse briefing.

    Layout:
    1. Header with date.
    2. Summary line with totals (open PRs, stale PRs, stale tickets).
    3. Flags grouped by urgency (critical, urgent, watch) up to max_flags.
    4. Each flag has details section + actions block with Approve and Dismiss buttons.
    5. Fallback 'all clear' message when 0 flags exist.
    6. Strict adherence to Slack limits (<= 50 blocks, <= 3000 chars per text block).

    Args:
        state: Current TeamPulseState.
        flags_with_db: List of dicts representing DB flags (must include 'id', 'item_id',
            'title', 'reason', 'urgency', 'suggested_action', 'evidence_age_days', 'evidence_author').
        max_flags: Optional limit on flags shown (defaults to settings.max_flags_in_briefing).
        now: Optional datetime for header date (defaults to UTC now).

    Returns:
        Dictionary with 'text' (fallback) and 'blocks' (Block Kit list).
    """
    settings = get_settings()
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc

    if now is not None:
        current_time = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        current_time = current_time.astimezone(tz)
    else:
        current_time = datetime.now(tz)

    limit = max_flags if max_flags is not None else settings.max_flags_in_briefing
    date_str = current_time.strftime("%A, %B %d, %Y")

    open_prs, stale_prs, stale_tickets = extract_totals(state)
    blocks: List[Dict[str, Any]] = []

    # 1. Header block
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"TeamPulse Ops Briefing — {date_str}",
            "emoji": True,
        },
    })

    # 2. Overview summary line
    summary_text = (
        f":bar_chart: *Overview:* {open_prs} open PRs ({stale_prs} stale) • "
        f"{stale_tickets} stale tickets"
    )
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": summary_text,
        },
    })
    blocks.append({"type": "divider"})

    # 3. Check for zero flags
    if not flags_with_db:
        all_clear_text = (
            ":white_check_mark: *All clear!* No stale pull requests or blocked "
            "tickets require team attention today."
        )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": all_clear_text,
            },
        })
        fallback = f"TeamPulse Briefing — {date_str}: All clear! No items require attention."
        return {"text": fallback, "blocks": blocks}

    # Group flags by urgency
    urgency_groups: Dict[str, List[Dict[str, Any]]] = {
        URGENCY_CRITICAL: [],
        URGENCY_URGENT: [],
        URGENCY_WATCH: [],
    }

    for item in flags_with_db:
        urg = (item.get("urgency") or URGENCY_WATCH).lower()
        if urg in urgency_groups:
            urgency_groups[urg].append(item)
        else:
            urgency_groups[URGENCY_WATCH].append(item)

    group_headers = {
        URGENCY_CRITICAL: ":rotating_light: *Critical Priority*",
        URGENCY_URGENT: ":warning: *Urgent Priority*",
        URGENCY_WATCH: ":eyes: *Watch / Monitoring*",
    }

    total_added = 0
    omitted_due_to_limit = 0
    included_flag_ids: List[int] = []

    # Process each urgency group in strict order
    for urg_key in [URGENCY_CRITICAL, URGENCY_URGENT, URGENCY_WATCH]:
        group_items = urgency_groups[urg_key]
        if not group_items:
            continue

        # Check if we've already hit the flag cap
        if total_added >= limit:
            omitted_due_to_limit += len(group_items)
            continue

        # Add urgency section header if room permits
        if len(blocks) + 3 >= SLACK_MAX_BLOCKS:
            omitted_due_to_limit += len(group_items)
            continue

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": group_headers[urg_key],
                }
            ],
        })

        for flag in group_items:
            if total_added >= limit:
                omitted_due_to_limit += 1
                continue

            # Need 2 blocks: 1 section, 1 actions
            if len(blocks) + 2 >= SLACK_MAX_BLOCKS:
                omitted_due_to_limit += 1
                continue

            db_id = flag.get("id")
            item_id = flag.get("item_id", "Unknown")
            title = flag.get("title") or "Item needs review"
            reason = flag.get("reason") or flag.get("flag_reason") or "Stale activity"
            suggested_action = flag.get("suggested_action") or "Review and take action"
            age_days = flag.get("evidence_age_days", flag.get("age_days", "N/A"))
            author = flag.get("evidence_author", flag.get("author_or_assignee", "Unknown"))

            item_link = format_item_link(item_id)
            flag_text_lines = [
                f"*{item_link}* — {title}",
                f"• *Reason:* {reason}",
                f"• *Evidence:* Age: {age_days}d | Author/Assignee: {author}",
                f"• *Suggested Action:* {suggested_action}",
            ]
            full_text = "\n".join(flag_text_lines)
            safe_text = truncate_slack_text(full_text)

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": safe_text,
                },
            })

            # Two buttons: Approve (primary) and Dismiss. Value is DB ID only!
            blocks.append({
                "type": "actions",
                "block_id": f"flag_actions_{db_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "approve_action",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve",
                            "emoji": True,
                        },
                        "style": "primary",
                        "value": str(db_id),
                    },
                    {
                        "type": "button",
                        "action_id": "dismiss_action",
                        "text": {
                            "type": "plain_text",
                            "text": "Dismiss",
                            "emoji": True,
                        },
                        "value": str(db_id),
                    },
                ],
            })

            if db_id is not None:
                included_flag_ids.append(db_id)
            total_added += 1

    if omitted_due_to_limit > 0 and len(blocks) < SLACK_MAX_BLOCKS:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_...and {omitted_due_to_limit} more item(s) omitted due to display limits._",
                }
            ],
        })

    fallback_text = (
        f"TeamPulse Ops Briefing — {date_str}: {total_added} item(s) need review."
    )

    return {"text": fallback_text, "blocks": blocks, "included_flag_ids": included_flag_ids}
