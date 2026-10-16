"""Slack Block Kit briefing builder from analysis results and database flags."""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from app.config import get_settings
from app.core.constants import URGENCY_CRITICAL, URGENCY_URGENT, URGENCY_WATCH
from app.core.errors import format_log_error
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


def clean_short_reason(reason: Optional[str], max_len: int = 80) -> str:
    """Format and clean a short reason string of at most max_len characters.

    Extracts a safe short status reason (e.g. 'HTTP 403 Forbidden').
    Never includes exception bodies, dicts, tokens, or the word 'details'.
    Logs the full error via the existing secret-masking logger.
    """
    if not reason:
        return "error during fetch"

    # Log full error with existing secret-masking logger, not in Slack text
    logger.warning("Data source error encountered: %s", format_log_error(reason))

    msg = str(reason)

    # 1. Strip raw dictionaries / JSON structures / curly or square brackets
    msg = re.sub(r"\{.*?\}", "", msg, flags=re.DOTALL)
    msg = re.sub(r"\[.*?\]", "", msg, flags=re.DOTALL)

    # 2. Strip "with details: ...", "(details: ...)", "details: ...", "details=..."
    msg = re.sub(r"\s*\(details:.*?\)", "", msg, flags=re.IGNORECASE | re.DOTALL)
    msg = re.sub(r"\s*\bwith\s+details\b.*$", "", msg, flags=re.IGNORECASE | re.DOTALL)
    msg = re.sub(r"\s*\bdetails\s*[:=].*$", "", msg, flags=re.IGNORECASE | re.DOTALL)

    # 3. Strip raw response fields or HTTP payload patterns
    msg = re.sub(r"'raw_text':\s*'.*?'", "", msg, flags=re.DOTALL)
    msg = re.sub(r"'raw_response':.*$", "", msg, flags=re.DOTALL)
    msg = re.sub(r"'response_json':.*$", "", msg, flags=re.DOTALL)

    # 4. Remove any remaining occurrences of "details" or token/secret identifiers
    msg = re.sub(r"\bdetails\b", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\bsecret_token\b.*", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\b(?:api_key|secret|token)\b.*", "", msg, flags=re.IGNORECASE)

    # 5. Remove any leftover braces/brackets
    msg = re.sub(r"[\{\}\[\]]", "", msg)

    cleaned = msg.replace("\n", " ").replace("\r", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:-")

    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned or "error during fetch"


def inspect_source_health(state: TeamPulseState) -> Tuple[bool, Optional[str], bool, Optional[str]]:
    """Determine whether GitHub and Jira sources succeeded or failed, and reasons if failed.

    Returns:
        Tuple of (gh_ok, gh_reason, jr_ok, jr_reason).
    """
    gh_ok = True
    gh_reason = None
    jr_ok = True
    jr_reason = None

    gh_data = getattr(state, "github_data", None)
    if isinstance(gh_data, dict) and (gh_data.get("status") == "unavailable" or "error" in gh_data):
        gh_ok = False
        gh_reason = gh_data.get("error") or gh_data.get("reason") or "collection failed"
    elif isinstance(gh_data, Exception):
        gh_ok = False
        gh_reason = str(gh_data)
    elif isinstance(gh_data, str) and ("error" in gh_data.lower() or "unavailable" in gh_data.lower()):
        gh_ok = False
        gh_reason = gh_data

    jr_data = getattr(state, "jira_data", None)
    if isinstance(jr_data, dict) and (jr_data.get("status") == "unavailable" or "error" in jr_data):
        jr_ok = False
        jr_reason = jr_data.get("error") or jr_data.get("reason") or "collection failed"
    elif isinstance(jr_data, Exception):
        jr_ok = False
        jr_reason = str(jr_data)
    elif isinstance(jr_data, str) and ("error" in jr_data.lower() or "unavailable" in jr_data.lower()):
        jr_ok = False
        jr_reason = jr_data

    errors = getattr(state, "errors", []) or []
    for err in errors:
        err_str = str(err)
        if "github" in err_str.lower() and ("unavailable" in err_str.lower() or "error" in err_str.lower() or "rate limit" in err_str.lower()):
            gh_ok = False
            if not gh_reason:
                gh_reason = err_str.split(":", 1)[-1].strip() if ":" in err_str else err_str
        if "jira" in err_str.lower() and ("unavailable" in err_str.lower() or "error" in err_str.lower() or "fail" in err_str.lower()):
            jr_ok = False
            if not jr_reason:
                jr_reason = err_str.split(":", 1)[-1].strip() if ":" in err_str else err_str

    return gh_ok, gh_reason, jr_ok, jr_reason


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
    5. Fallback 'all clear' message when 0 flags exist and all sources succeeded.
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

    gh_ok, gh_reason, jr_ok, jr_reason = inspect_source_health(state)
    warning_lines: List[str] = []
    if not gh_ok:
        short_reason = clean_short_reason(gh_reason, max_len=80)
        warning_lines.append(f"WARNING: GitHub data unavailable ({short_reason}). This briefing may be incomplete.")
    if not jr_ok:
        short_reason = clean_short_reason(jr_reason, max_len=80)
        warning_lines.append(f"WARNING: Jira data unavailable ({short_reason}). This briefing may be incomplete.")

    open_prs, stale_prs, stale_tickets = extract_totals(state)
    blocks: List[Dict[str, Any]] = []

    # Warning blocks at the very top if any data source failed
    for w in warning_lines:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": w,
            },
        })

    # 1. Header block
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"TeamPulse Ops Briefing — {date_str}",
            "emoji": True,
        },
    })

    # 2. Overview summary line (never show '0 open PRs' if GitHub failed, show 'unknown')
    open_prs_str = str(open_prs) if gh_ok else "unknown"
    stale_prs_str = str(stale_prs) if gh_ok else "unknown"
    stale_tickets_str = str(stale_tickets) if jr_ok else "unknown"

    summary_text = (
        f":bar_chart: *Overview:* {open_prs_str} open PRs ({stale_prs_str} stale) • "
        f"{stale_tickets_str} stale tickets"
    )
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": summary_text,
        },
    })
    blocks.append({"type": "divider"})

    warning_prefix = "\n".join(warning_lines)

    # 3. Check for zero flags
    if not flags_with_db:
        if gh_ok and jr_ok:
            if stale_prs == 0 and stale_tickets == 0:
                all_clear_text = (
                    ":white_check_mark: *All clear!* No stale pull requests or blocked "
                    "tickets require team attention today."
                )
                tail = "All clear! No items require attention."
            else:
                stale_msg = f"No new items since the last briefing. {stale_prs} PRs and {stale_tickets} tickets are still stale."
                all_clear_text = f":information_source: {stale_msg}"
                tail = stale_msg
        else:
            all_clear_text = ":information_source: No new items flagged from available sources. Incomplete data due to source unavailability."
            tail = "Data collection was incomplete."

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": all_clear_text,
            },
        })
        base_fallback = f"TeamPulse Briefing — {date_str} (Overview: {open_prs_str} open PRs ({stale_prs_str} stale) • {stale_tickets_str} stale tickets): {tail}"
        fallback = f"{warning_prefix}\n{base_fallback}".strip() if warning_prefix else base_fallback
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
    if warning_prefix:
        fallback_text = f"{warning_prefix}\n{fallback_text}"

    return {"text": fallback_text, "blocks": blocks, "included_flag_ids": included_flag_ids}
