"""Analyze agent node: prompt construction, LLM router invocation, validation, deduplication, grounding, and fallback."""

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import select

from app.config import get_settings
from app.core.errors import AnalysisError
from app.db.database import get_db_session
from app.db.models import Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, PriorityFlag

logger = logging.getLogger(__name__)

# Regular expressions for ID detection in text
JIRA_KEY_REGEX = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
PR_REGEX = re.compile(r"(?:[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)?#(\d+)")


def build_analysis_prompt(
    workspace_id: str,
    prs: List[PullRequest],
    tickets: List[JiraIssue],
    recent_flags: List[Flag],
    stale_pr_days: int,
    stale_ticket_days: int,
) -> str:
    """Construct an LLM prompt from normalized text sections rather than raw JSON.

    Args:
        workspace_id: Team workspace identifier.
        prs: List of open GitHub pull requests.
        tickets: List of open Jira tickets.
        recent_flags: Flags created within the last 24 hours.
        stale_pr_days: Threshold days for stale PRs.
        stale_ticket_days: Threshold days for stale tickets.

    Returns:
        Structured plain text prompt.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Section 1: Role and Context
    prompt_lines = [
        "### ROLE AND DATE",
        "You are TeamPulse, an AI Operations Agent for engineering teams.",
        f"Workspace: {workspace_id}",
        f"Today's Date: {today_str} (UTC)",
        "",
        "### OPERATIONAL THRESHOLDS",
        f"- Stale PR Threshold: {stale_pr_days} days of inactivity",
        f"- Stale Jira Ticket Threshold: {stale_ticket_days} days without updates",
        "",
    ]

    # Section 2: GitHub Pull Requests
    prompt_lines.append("### GITHUB OPEN PULL REQUESTS")
    if not prs:
        prompt_lines.append("No open pull requests found.")
    else:
        for pr in prs:
            reviewers_str = ", ".join(pr.reviewers) if pr.reviewers else "None"
            stale_tag = "STALE" if pr.is_stale else "ACTIVE"
            prompt_lines.append(
                f"- [{stale_tag}] {pr.repo} PR #{pr.number or pr.id}: {pr.title}\n"
                f"  Author: {pr.author} | Reviewers: {reviewers_str} | "
                f"Age: {pr.age_days}d | Last Commit: {pr.last_commit_days}d ago"
            )
    prompt_lines.append("")

    # Section 3: Jira Issues
    prompt_lines.append("### JIRA OPEN TICKETS")
    if not tickets:
        prompt_lines.append("No open Jira tickets found.")
    else:
        for ticket in tickets:
            assignee_str = ticket.assignee or "Unassigned"
            stale_tag = "STALE" if ticket.is_stale else "ACTIVE"
            prompt_lines.append(
                f"- [{stale_tag}] [{ticket.key}] {ticket.title}\n"
                f"  Status: {ticket.status} | Assignee: {assignee_str} | "
                f"Age: {ticket.age_days}d | Inactive: {ticket.days_since_update}d | "
                f"Comments: {ticket.comment_count}"
            )
    prompt_lines.append("")

    # Section 4: Previously Flagged Items (Last 24 Hours)
    prompt_lines.append("### PREVIOUSLY FLAGGED ITEMS (LAST 24 HOURS)")
    if not recent_flags:
        prompt_lines.append("None. No items flagged in the last 24 hours.")
    else:
        for flag in recent_flags:
            prompt_lines.append(
                f"- {flag.item_id} ({flag.item_type}) | Level: {flag.flag_level} | Reason: {flag.flag_reason}"
            )
        prompt_lines.append(
            "\nIMPORTANT DEDUPLICATION INSTRUCTION: The items listed above were already flagged "
            "and briefed to the team within the last 24 hours. Do NOT re-surface them in priority_flags "
            "unless they have escalated in severity or urgency since then."
        )
    prompt_lines.append("")

    # Section 5: Goal and Grounding Instructions
    prompt_lines.extend(
        [
            "### GOAL",
            "Synthesize the state of engineering work, identify stalled work or delivery bottlenecks,",
            "extract priority flags that need attention, note completed items if any, and provide one clear recommendation.",
            "Only reference item IDs that appear in the data. If nothing needs attention, say so. Never invent IDs.",
            "Output must strictly follow the JSON schema.",
        ]
    )

    return "\n".join(prompt_lines)


def extract_valid_item_ids(prs: List[PullRequest], tickets: List[JiraIssue]) -> Set[str]:
    """Build the set of valid item IDs from collected GitHub PRs and Jira issues.

    Args:
        prs: Collected pull requests.
        tickets: Collected Jira issues.

    Returns:
        Set of valid string identifiers.
    """
    valid_ids: Set[str] = set()
    for t in tickets:
        if t.key:
            valid_ids.add(t.key)
            valid_ids.add(t.key.upper())
            valid_ids.add(t.key.lower())
    for p in prs:
        pr_num = str(p.number) if p.number is not None else ""
        pr_id = str(p.id) if p.id is not None else ""
        if pr_num:
            valid_ids.add(f"#{pr_num}")
            valid_ids.add(pr_num)
            if p.repo:
                valid_ids.add(f"{p.repo}#{pr_num}")
                valid_ids.add(f"{p.repo}/pull/{pr_num}")
        if pr_id:
            valid_ids.add(f"#{pr_id}")
            valid_ids.add(pr_id)
            if p.repo:
                valid_ids.add(f"{p.repo}#{pr_id}")
                valid_ids.add(f"{p.repo}/pull/{pr_id}")
    return valid_ids


def is_text_grounded(text: str, valid_ids: Set[str]) -> bool:
    """Return False if text contains any ID-like string that is not in valid_ids.

    Args:
        text: String content to inspect.
        valid_ids: Set of valid collected IDs.

    Returns:
        True if all ID-like mentions are grounded in valid_ids, False otherwise.
    """
    if not text:
        return True

    # Check Jira keys (e.g. KAN-42, ABC-123)
    for m in JIRA_KEY_REGEX.finditer(text):
        key = m.group(0)
        if key not in valid_ids and key.upper() not in valid_ids:
            return False

    # Check PR references (e.g. #47, owner/repo#47)
    for m in PR_REGEX.finditer(text):
        full_match = m.group(0)
        num = m.group(1)
        if (
            full_match not in valid_ids
            and f"#{num}" not in valid_ids
            and num not in valid_ids
        ):
            return False

    return True


def generate_stale_fallback_sentence(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
    field_type: str = "recommendation",
) -> str:
    """Construct a rule-based sentence referencing only real stale work items.

    Args:
        prs: Collected pull requests.
        tickets: Collected Jira issues.
        field_type: One of 'recommendation', 'velocity', or 'pattern'.

    Returns:
        Deterministic rule-based sentence.
    """
    stale_items: List[str] = []
    for t in tickets:
        if getattr(t, "is_stale", False):
            stale_items.append(t.key)
    for p in prs:
        if getattr(p, "is_stale", False):
            pr_label = f"{p.repo} PR #{p.number}" if p.repo and p.number else f"PR #{p.number or p.id}"
            stale_items.append(pr_label)

    if not stale_items:
        if field_type == "recommendation":
            return "No critical blockers detected; keep current sprint velocity."
        elif field_type == "velocity":
            return "All active work items are progressing within standard operational thresholds."
        else:
            return "All active work items are progressing within standard operational thresholds."

    items_str = ", ".join(stale_items)
    if field_type == "recommendation":
        return f"Focus on unblocking stalled work items: {items_str}."
    elif field_type == "velocity":
        return f"Sprint velocity impacted by stalled items: {items_str}."
    else:
        return f"Stalled work items requiring team attention: {items_str}."


def add_state_error(state: Any, message: str) -> None:
    """Append an operational error or warning message to state errors list.

    Args:
        state: State dictionary or TeamPulseState model.
        message: Warning or error string.
    """
    if isinstance(state, dict):
        if "errors" not in state or not isinstance(state["errors"], list):
            state["errors"] = []
        state["errors"].append(message)
    elif hasattr(state, "errors"):
        if state.errors is None:
            state.errors = []
        state.errors.append(message)


def get_recent_flags(workspace_id: str) -> List[Flag]:
    """Retrieve flags for the workspace created in the last 24 hours.

    Args:
        workspace_id: Team workspace identifier.

    Returns:
        List of Flag ORM models.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    try:
        with get_db_session() as session:
            stmt = select(Flag).where(
                Flag.workspace_id == workspace_id,
                Flag.created_at >= cutoff,
            )
            flags = list(session.scalars(stmt).all())
            session.expunge_all()
            return flags
    except Exception as exc:
        logger.warning("Could not query recent flags from database: %s", exc)
        return []


def save_flags_to_db(workspace_id: str, priority_flags: List[PriorityFlag]) -> None:
    """Persist new priority flags to the database for future deduplication.

    Args:
        workspace_id: Team workspace identifier.
        priority_flags: List of generated PriorityFlag items.
    """
    if not priority_flags:
        return

    try:
        with get_db_session() as session:
            for flag in priority_flags:
                flag_record = Flag(
                    workspace_id=workspace_id,
                    item_type=flag.source,
                    item_id=flag.item_id,
                    flag_reason=flag.reason,
                    flag_level=flag.urgency,
                )
                session.add(flag_record)
        logger.info("Saved %d new flags to database for workspace %s", len(priority_flags), workspace_id)
    except Exception as exc:
        logger.warning("Failed to save flags to database: %s", exc)


def build_rule_based_analysis(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
    recent_flag_ids: Set[str],
) -> Analysis:
    """Deterministic rule-based fallback summary when LLM generation fails twice.

    Args:
        prs: Collected pull requests.
        tickets: Collected Jira issues.
        recent_flag_ids: IDs of items already surfaced in the last 24h.

    Returns:
        Rule-based Analysis instance.
    """
    stale_prs = [p for p in prs if getattr(p, "is_stale", False) and f"{p.repo}#{p.number or p.id}" not in recent_flag_ids]
    stale_tickets = [t for t in tickets if getattr(t, "is_stale", False) and t.key not in recent_flag_ids]

    priority_flags: List[PriorityFlag] = []

    for pr in stale_prs:
        reviewers_str = ", ".join(pr.reviewers) if pr.reviewers else "reviewers"
        priority_flags.append(
            PriorityFlag(
                title=f"Stale PR: {pr.title}",
                reason=f"Open for {pr.age_days}d without commits in {pr.last_commit_days}d",
                urgency="watch" if pr.age_days < 14 else "urgent",
                source="github",
                item_id=f"{pr.repo}#{pr.number or pr.id}",
                suggested_action=f"Ping {reviewers_str} to complete review or close if obsolete",
            )
        )

    for t in stale_tickets:
        assignee_str = t.assignee or "team lead"
        priority_flags.append(
            PriorityFlag(
                title=f"Stalled Ticket: [{t.key}] {t.title}",
                reason=f"Inactive for {t.days_since_update} days in status '{t.status}'",
                urgency="watch" if t.days_since_update < 21 else "urgent",
                source="jira",
                item_id=t.key,
                suggested_action=f"Check in with {assignee_str} on status blockers",
            )
        )

    patterns = []
    if stale_prs:
        patterns.append(f"{len(stale_prs)} pull request(s) exceed stale threshold.")
    if stale_tickets:
        patterns.append(f"{len(stale_tickets)} Jira ticket(s) have had no activity past threshold.")
    if not patterns:
        patterns.append("All active work items are progressing within standard operational thresholds.")

    recommendation = (
        "Focus on unblocking stagnant pull requests and confirming next steps on inactive tickets."
        if priority_flags
        else "No critical blockers detected; keep current sprint velocity."
    )

    return Analysis(
        patterns=patterns,
        priority_flags=priority_flags,
        shipped_this_period=[],
        velocity_note="Generated via rule-based fallback summary (LLM unavailable).",
        one_recommendation=recommendation,
    )


async def analyze_workspace(
    state: TeamPulseState,
    router: Optional[LLMRouter] = None,
) -> Tuple[Analysis, str, float]:
    """Execute analysis step: prompt build, LLM call, re-prompt on invalid JSON, grounding check, or rule fallback.

    Args:
        state: Current TeamPulseState.
        router: Optional LLMRouter instance.

    Returns:
        Tuple of (Analysis, model_name, latency_ms).
    """
    settings = get_settings()
    llm_router = router or LLMRouter()

    # Extract collected items
    raw_prs = state.github_data if hasattr(state, "github_data") else state.get("github_data", [])
    raw_tickets = state.jira_data if hasattr(state, "jira_data") else state.get("jira_data", [])

    prs: List[PullRequest] = []
    if isinstance(raw_prs, list):
        for item in raw_prs:
            if isinstance(item, PullRequest):
                prs.append(item)
            elif isinstance(item, dict):
                prs.append(PullRequest.model_validate(item))

    tickets: List[JiraIssue] = []
    if isinstance(raw_tickets, list):
        for item in raw_tickets:
            if isinstance(item, JiraIssue):
                tickets.append(item)
            elif isinstance(item, dict):
                tickets.append(JiraIssue.model_validate(item))

    # 5. Check if there are zero stale or at-risk items: skip LLM call entirely
    stale_prs = [p for p in prs if getattr(p, "is_stale", False)]
    stale_tickets = [t for t in tickets if getattr(t, "is_stale", False)]

    if not stale_prs and not stale_tickets:
        logger.info("Zero stale or at-risk items detected. Skipping LLM call.")
        clean_analysis = Analysis(
            patterns=["All active work items are progressing within standard operational thresholds."],
            priority_flags=[],
            shipped_this_period=[],
            velocity_note="No stalled PRs or inactive tickets detected across repositories.",
            one_recommendation="No items need immediate attention; team work is moving normally.",
        )
        return clean_analysis, "skipped_zero_stale_items", 0.0

    workspace_id = state.workspace_id if hasattr(state, "workspace_id") else state.get("workspace_id", "default")

    # Read recent flags from database
    recent_flags = get_recent_flags(workspace_id)
    recent_flag_ids = {f.item_id for f in recent_flags}

    prompt = build_analysis_prompt(
        workspace_id=workspace_id,
        prs=prs,
        tickets=tickets,
        recent_flags=recent_flags,
        stale_pr_days=settings.stale_pr_days,
        stale_ticket_days=settings.stale_ticket_days,
    )

    analysis: Optional[Analysis] = None
    model_name = "rule_based_fallback"
    latency_ms = 0.0

    # Attempt 1: Standard analysis request
    try:
        result = await llm_router.route(
            prompt=prompt,
            schema=Analysis,
            task_type="analysis",
            system_prompt="You are TeamPulse. Output strictly valid JSON matching the schema.",
        )
        analysis = result.data
        model_name = result.model_name
        latency_ms = result.latency_ms
    except Exception as first_err:
        logger.warning("First analysis attempt failed: %s. Re-prompting with schema reminder...", first_err)

        # Attempt 2: Re-prompt once with strict schema reminder and grounding instruction
        schema_json = json.dumps(Analysis.model_json_schema(), indent=2)
        reprompt = (
            f"{prompt}\n\n"
            f"### CRITICAL RE-PROMPT INSTRUCTION\n"
            f"Your previous response was rejected due to an invalid schema or malformed JSON.\n"
            f"Only reference item IDs that appear in the data. If nothing needs attention, say so. Never invent IDs.\n"
            f"You MUST return ONLY valid JSON matching this exact JSON schema:\n{schema_json}"
        )

        try:
            result = await llm_router.route(
                prompt=reprompt,
                schema=Analysis,
                task_type="analysis",
                system_prompt="Return ONLY raw JSON conforming to the schema. No markdown, no prose.",
            )
            analysis = result.data
            model_name = result.model_name
            latency_ms = result.latency_ms
        except Exception as second_err:
            logger.error("Second analysis attempt failed: %s. Engaging rule-based fallback.", second_err)
            analysis = build_rule_based_analysis(prs, tickets, recent_flag_ids)
            model_name = "rule_based_fallback"
            latency_ms = 0.0

    # Grounding Check 1: Build valid item IDs from collected data
    valid_ids = extract_valid_item_ids(prs, tickets)

    # Grounding Check 2: Drop any priority_flag whose item_id is not in valid_ids, and add warning to state["errors"]
    grounded_flags: List[PriorityFlag] = []
    for flag in analysis.priority_flags:
        clean_id = flag.item_id.strip()
        is_valid = (
            clean_id in valid_ids
            or clean_id.upper() in valid_ids
            or clean_id.strip("[]") in valid_ids
        )
        if not is_valid:
            warning_msg = f"Dropped ungrounded priority flag with item_id: {flag.item_id}"
            logger.warning(warning_msg)
            add_state_error(state, warning_msg)
            continue

        # Filter out recent flags from analysis if LLM accidentally included un-escalated ones
        if flag.item_id in recent_flag_ids and flag.urgency == "watch":
            logger.debug("Skipping duplicate watch flag: %s", flag.item_id)
            continue

        grounded_flags.append(flag)

    analysis.priority_flags = grounded_flags

    # Grounding Check 3: Scan text of one_recommendation, velocity_note, patterns for ungrounded IDs
    if not is_text_grounded(analysis.one_recommendation, valid_ids):
        logger.warning(
            "Ungrounded ID found in one_recommendation ('%s'). Replacing with rule-based sentence.",
            analysis.one_recommendation,
        )
        analysis.one_recommendation = generate_stale_fallback_sentence(
            prs, tickets, field_type="recommendation"
        )

    if not is_text_grounded(analysis.velocity_note, valid_ids):
        logger.warning(
            "Ungrounded ID found in velocity_note ('%s'). Replacing with rule-based sentence.",
            analysis.velocity_note,
        )
        analysis.velocity_note = generate_stale_fallback_sentence(
            prs, tickets, field_type="velocity"
        )

    grounded_patterns: List[str] = []
    for pattern in analysis.patterns:
        if is_text_grounded(pattern, valid_ids):
            grounded_patterns.append(pattern)
        else:
            logger.warning(
                "Ungrounded ID found in pattern ('%s'). Replacing with rule-based sentence.",
                pattern,
            )
            grounded_patterns.append(
                generate_stale_fallback_sentence(prs, tickets, field_type="pattern")
            )
    analysis.patterns = grounded_patterns

    # Save new priority flags to database for deduplication on next run
    save_flags_to_db(workspace_id, analysis.priority_flags)

    return analysis, model_name, latency_ms
