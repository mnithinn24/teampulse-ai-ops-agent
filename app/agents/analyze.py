"""Analyze agent node: prompt construction, LLM router invocation, validation, deduplication, grounding, and fallback."""

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from sqlalchemy import select

from app.config import get_settings
from app.core.constants import (
    FLAG_LEVEL_ESCALATED,
    FLAG_LEVEL_NORMAL,
    ITEM_TYPE_GITHUB_PR,
    ITEM_TYPE_JIRA_ISSUE,
    URGENCY_CRITICAL,
    URGENCY_URGENT,
    URGENCY_WATCH,
)
from app.core.errors import AnalysisError, InvalidJSONError
from app.core.ids import (
    FULL_PR_REGEX,
    JIRA_KEY_REGEX,
    PARTIAL_PR_REGEX,
    canonical_id,
    normalize_llm_id,
)
from app.db.database import get_db_session
from app.db.models import Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, FlagEvidence, PriorityFlag

logger = logging.getLogger(__name__)


def build_analysis_prompt(
    workspace_id: str,
    prs: List[PullRequest],
    tickets: List[JiraIssue],
    recent_flags: List[Flag],
    stale_pr_days: int,
    stale_ticket_days: int,
    max_items_in_prompt: int = 15,
    abandoned_pr_days: int = 180,
) -> str:
    """Construct an LLM prompt from normalized text sections rather than raw JSON.

    Limits stale PRs and Jira issues to max_items_in_prompt sorted by age descending,
    showing canonical item IDs and overall counts. Contains no hardcoded or fake example IDs.

    Args:
        workspace_id: Team workspace identifier.
        prs: List of open GitHub pull requests.
        tickets: List of open Jira tickets.
        recent_flags: Flags created within the last 24 hours.
        stale_pr_days: Threshold days for stale PRs.
        stale_ticket_days: Threshold days for stale tickets.
        max_items_in_prompt: Maximum number of stale items per category to include.
        abandoned_pr_days: Days after which a PR is considered abandoned.

    Returns:
        Structured plain text prompt.
    """
    settings = get_settings()
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    today_str = datetime.now(tz).strftime("%Y-%m-%d")

    # Section 1: Role and Context
    prompt_lines = [
        "### ROLE AND DATE",
        "You are TeamPulse, an AI Operations Agent for engineering teams.",
        f"Workspace: {workspace_id}",
        f"Today's Date: {today_str} ({settings.timezone})",
        "",
        "### OPERATIONAL THRESHOLDS",
        f"- Stale PR Threshold: {stale_pr_days} days of inactivity",
        f"- Stale Jira Ticket Threshold: {stale_ticket_days} days without updates",
        f"- Abandoned PR Threshold: {abandoned_pr_days} days of total age",
        "",
    ]

    # Section 2: GitHub Pull Requests (Stale items sorted by age descending, limited to max_items_in_prompt)
    stale_prs = sorted(
        [p for p in prs if getattr(p, "is_stale", False)],
        key=lambda p: p.age_days,
        reverse=True,
    )
    total_stale_prs = len(stale_prs)
    showing_prs = stale_prs[:max_items_in_prompt]

    prompt_lines.append("### GITHUB STALE PULL REQUESTS")
    if not total_stale_prs:
        prompt_lines.append("0 stale PRs in total. No stale pull requests found.")
    else:
        prompt_lines.append(f"{total_stale_prs} stale PRs in total, showing the {len(showing_prs)} oldest:")
        for pr in showing_prs:
            canon_id = canonical_id("github", pr.number or pr.id, repo=pr.repo)
            reviewers_str = ", ".join(pr.reviewers) if pr.reviewers else "None"
            prompt_lines.append(
                f"- [STALE] {canon_id}: {pr.title}\n"
                f"  Author: {pr.author} | Reviewers: {reviewers_str} | "
                f"Age: {pr.age_days}d | Inactive: {pr.last_commit_days}d"
            )
    prompt_lines.append("")

    # Section 3: Jira Issues (Stale items sorted by age descending, limited to max_items_in_prompt)
    stale_tickets = sorted(
        [t for t in tickets if getattr(t, "is_stale", False)],
        key=lambda t: t.age_days,
        reverse=True,
    )
    total_stale_tickets = len(stale_tickets)
    showing_tickets = stale_tickets[:max_items_in_prompt]

    prompt_lines.append("### JIRA STALE TICKETS")
    if not total_stale_tickets:
        prompt_lines.append("0 stale Jira issues in total. No stale Jira tickets found.")
    else:
        prompt_lines.append(f"{total_stale_tickets} stale Jira issues in total, showing the {len(showing_tickets)} oldest:")
        for ticket in showing_tickets:
            canon_id = canonical_id("jira", ticket.key)
            assignee_str = ticket.assignee or "Unassigned"
            prompt_lines.append(
                f"- [STALE] {canon_id}: {ticket.title}\n"
                f"  Assignee: {assignee_str} | Status: {ticket.status} | "
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

    # Section 5: Goal and Grounding Instructions (strictly placeholder-driven, NO fake example IDs)
    prompt_lines.extend(
        [
            "### GOAL AND INSTRUCTIONS",
            "Synthesize the state of engineering work, identify stalled work or delivery bottlenecks,",
            "extract priority flags that need attention, note completed items if any, and provide one clear recommendation.",
            "",
            "CRITICAL GROUNDING AND EVIDENCE RULES:",
            "1. item_id: Copy the exact canonical ID from the data above into item_id (schema placeholder: '<copy an ID exactly from the lists above>'). Never invent IDs or use bare numbers.",
            "2. evidence: For every priority flag, provide an evidence object with:",
            "   - age_days: integer age in days copied directly from the data",
            "   - author_or_assignee: string author or assignee copied directly from the data",
            "3. reason: Must only use facts present in the data (title, age, author, review state, status).",
            "   Never invent unsupported claims (for example, do not claim an item is 'security-sensitive' or a 'vulnerability' unless explicitly in title or labels).",
            f"4. Abandoned PRs: PRs older than {abandoned_pr_days} days must have urgency 'watch' and suggested_action 'Ask the author if still active, otherwise close'. They must never be 'urgent' or 'critical' merely for age.",
            "5. one_recommendation: Must explicitly name at least one real canonical ID from the data.",
            "6. Output must strictly follow the JSON schema.",
        ]
    )

    return "\n".join(prompt_lines)


def has_unsupported_claim(reason: str, item: Union[PullRequest, JiraIssue]) -> bool:
    """Check if the flag reason introduces sensitive or severe claims not present in the data.

    Args:
        reason: LLM-generated reason string.
        item: Collected PullRequest or JiraIssue object.

    Returns:
        True if reason makes claims absent from item metadata, False otherwise.
    """
    reason_lower = reason.lower()
    title = (getattr(item, "title", "") or "").lower()
    labels = " ".join([str(l).lower() for l in getattr(item, "labels", [])])
    searchable_text = f"{title} {labels}"

    unsupported_keywords = [
        "security",
        "vulnerability",
        "cve",
        "exploit",
        "credential",
        "leak",
        "memory spike",
        "data loss",
        "incident",
        "p0",
        "sev1",
        "outage",
    ]
    for kw in unsupported_keywords:
        if kw in reason_lower and kw not in searchable_text:
            return True

    return False


def extract_canonical_ids_from_text(text: str, valid_canonical_ids: Set[str]) -> List[str]:
    """Find all canonical IDs mentioned in a text block that resolve to valid IDs.

    Args:
        text: String content (e.g. recommendation).
        valid_canonical_ids: Set of valid canonical IDs.

    Returns:
        List of matched valid canonical IDs.
    """
    found: List[str] = []
    for m in FULL_PR_REGEX.finditer(text):
        token = m.group(0)
        norm = normalize_llm_id(token, valid_canonical_ids)
        if norm and norm not in found:
            found.append(norm)

    for m in PARTIAL_PR_REGEX.finditer(text):
        token = m.group(0)
        norm = normalize_llm_id(token, valid_canonical_ids)
        if norm and norm not in found:
            found.append(norm)

    for m in JIRA_KEY_REGEX.finditer(text):
        token = m.group(1)
        norm = normalize_llm_id(token, valid_canonical_ids)
        if norm and norm not in found:
            found.append(norm)

    return found


def is_text_grounded(text: str, valid_canonical_ids: Set[str]) -> bool:
    """Return False if text contains any ID-like string that cannot be normalized to a valid canonical ID.

    Args:
        text: String content to inspect.
        valid_canonical_ids: Set of valid canonical IDs.

    Returns:
        True if all ID-like mentions normalize to valid IDs, False otherwise.
    """
    if not text:
        return True

    for m in JIRA_KEY_REGEX.finditer(text):
        token = m.group(1)
        if normalize_llm_id(token, valid_canonical_ids) is None:
            return False

    for m in FULL_PR_REGEX.finditer(text):
        token = m.group(0)
        if normalize_llm_id(token, valid_canonical_ids) is None:
            return False

    for m in PARTIAL_PR_REGEX.finditer(text):
        token = m.group(0)
        if normalize_llm_id(token, valid_canonical_ids) is None:
            return False

    return True


def generate_stale_fallback_sentence(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
    field_type: str = "recommendation",
) -> str:
    """Construct a rule-based sentence referencing only real stale work items using canonical IDs.

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
            stale_items.append(canonical_id("jira", t.key))
    for p in prs:
        if getattr(p, "is_stale", False):
            stale_items.append(canonical_id("github", p.number or p.id, repo=p.repo))

    if not stale_items:
        if field_type == "recommendation":
            return "No critical blockers detected; keep current sprint velocity."
        elif field_type == "velocity":
            return "All active work items are progressing within standard operational thresholds."
        else:
            return "All active work items are progressing within standard operational thresholds."

    items_str = ", ".join(stale_items[:3])
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
                Flag.slack_ts.is_not(None),
            )
            flags = list(session.scalars(stmt).all())
            session.expunge_all()
            return flags
    except Exception as exc:
        logger.warning("Could not query recent flags from database: %s", exc)
        return []


def build_rule_based_analysis(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
    recent_flag_ids: Set[str],
) -> Analysis:
    """Deterministic rule-based fallback summary when LLM generation fails.

    Args:
        prs: Collected pull requests.
        tickets: Collected Jira issues.
        recent_flag_ids: IDs of items already surfaced in the last 24h.

    Returns:
        Rule-based Analysis instance.
    """
    settings = get_settings()
    abandoned_threshold = settings.abandoned_pr_days

    stale_prs = [
        p
        for p in prs
        if getattr(p, "is_stale", False)
        and canonical_id("github", p.number or p.id, repo=p.repo) not in recent_flag_ids
    ]
    stale_tickets = [
        t
        for t in tickets
        if getattr(t, "is_stale", False)
        and canonical_id("jira", t.key) not in recent_flag_ids
    ]

    priority_flags: List[PriorityFlag] = []

    for pr in stale_prs:
        reviewers_str = ", ".join(pr.reviewers) if pr.reviewers else "reviewers"
        canon_id = canonical_id("github", pr.number or pr.id, repo=pr.repo)

        if pr.age_days >= abandoned_threshold:
            urgency = URGENCY_WATCH
            action = "Ask the author if still active, otherwise close"
        else:
            urgency = URGENCY_WATCH if pr.age_days < 14 else URGENCY_URGENT
            action = f"Ping {reviewers_str} to complete review or close if obsolete"

        priority_flags.append(
            PriorityFlag(
                title=f"Stale PR: {pr.title}",
                reason=f"Open for {pr.age_days}d by {pr.author} without commits in {pr.last_commit_days}d.",
                urgency=urgency,
                source=ITEM_TYPE_GITHUB_PR,
                item_id=canon_id,
                suggested_action=action,
                evidence=FlagEvidence(age_days=pr.age_days, author_or_assignee=pr.author),
            )
        )

    for t in stale_tickets:
        assignee_str = t.assignee or "team lead"
        canon_id = canonical_id("jira", t.key)
        priority_flags.append(
            PriorityFlag(
                title=f"Stalled Ticket: [{t.key}] {t.title}",
                reason=f"Inactive for {t.days_since_update}d in status '{t.status}' assigned to {assignee_str}.",
                urgency=URGENCY_WATCH if t.days_since_update < 21 else URGENCY_URGENT,
                source=ITEM_TYPE_JIRA_ISSUE,
                item_id=canon_id,
                suggested_action=f"Check in with {assignee_str} on status blockers",
                evidence=FlagEvidence(age_days=t.age_days, author_or_assignee=t.assignee or ""),
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
        generate_stale_fallback_sentence(prs, tickets, field_type="recommendation")
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

    # Check if there are zero stale or at-risk items: skip LLM call entirely
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

    # Build valid canonical IDs and lookup map
    valid_canonical_ids: Set[str] = set()
    item_map: Dict[str, Union[PullRequest, JiraIssue]] = {}

    for p in prs:
        cid = canonical_id("github", p.number or p.id, repo=p.repo)
        valid_canonical_ids.add(cid)
        item_map[cid] = p

    for t in tickets:
        if t.key:
            cid = canonical_id("jira", t.key)
            valid_canonical_ids.add(cid)
            item_map[cid] = t

    prompt = build_analysis_prompt(
        workspace_id=workspace_id,
        prs=prs,
        tickets=tickets,
        recent_flags=recent_flags,
        stale_pr_days=settings.stale_pr_days,
        stale_ticket_days=settings.stale_ticket_days,
        max_items_in_prompt=settings.max_items_in_prompt,
        abandoned_pr_days=settings.abandoned_pr_days,
    )

    candidates = await llm_router.get_candidates(task_type="analysis")
    analysis: Optional[Analysis] = None
    model_name = "rule_based_fallback"
    latency_ms = 0.0

    use_legacy_route = False
    if not isinstance(candidates, list) or any(not isinstance(c, (list, tuple)) for c in candidates):
        use_legacy_route = True

    if use_legacy_route:
        try:
            logger.info("Attempting analysis with router route")
            result = await llm_router.route(
                prompt=prompt,
                schema=Analysis,
                task_type="analysis",
                system_prompt="You are TeamPulse. Output strictly valid JSON matching the schema.",
            )
            analysis = result.data
            model_name = result.model_name
            latency_ms = result.latency_ms
        except InvalidJSONError as json_err:
            logger.warning(
                "Router returned invalid JSON: %s. Re-prompting with schema reminder...",
                json_err,
            )
            schema_json = json.dumps(Analysis.model_json_schema(), indent=2)
            reprompt = (
                f"{prompt}\n\n"
                f"### CRITICAL RE-PROMPT INSTRUCTION\n"
                f"Your previous response was rejected due to an invalid schema or malformed JSON.\n"
                f"Only reference item IDs that appear in the data above (e.g. '<copy an ID exactly from the lists above>').\n"
                f"Never invent IDs or claims not present in the data.\n"
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
            except Exception as reprompt_err:
                logger.warning("Re-prompt failed: %s.", reprompt_err)
        except Exception as provider_err:
            logger.warning(
                "Router failed with provider error (%s). Proceeding without re-prompt.",
                provider_err,
            )
    else:
        for provider, candidate_model in candidates:
            try:
                logger.info("Attempting analysis with candidate %s (%s)", provider, candidate_model)
                result = await llm_router.call_candidate(
                    provider=provider,
                    model_name=candidate_model,
                    prompt=prompt,
                    schema=Analysis,
                    task_type="analysis",
                    system_prompt="You are TeamPulse. Output strictly valid JSON matching the schema.",
                )
                analysis = result.data
                model_name = result.model_name
                latency_ms = result.latency_ms
                break
            except InvalidJSONError as json_err:
                logger.warning(
                    "Candidate %s (%s) returned invalid JSON: %s. Re-prompting with schema reminder...",
                    provider,
                    candidate_model,
                    json_err,
                )
                schema_json = json.dumps(Analysis.model_json_schema(), indent=2)
                reprompt = (
                    f"{prompt}\n\n"
                    f"### CRITICAL RE-PROMPT INSTRUCTION\n"
                    f"Your previous response was rejected due to an invalid schema or malformed JSON.\n"
                    f"Only reference item IDs that appear in the data above (e.g. '<copy an ID exactly from the lists above>').\n"
                    f"Never invent IDs or claims not present in the data.\n"
                    f"You MUST return ONLY valid JSON matching this exact JSON schema:\n{schema_json}"
                )
                try:
                    result = await llm_router.call_candidate(
                        provider=provider,
                        model_name=candidate_model,
                        prompt=reprompt,
                        schema=Analysis,
                        task_type="analysis",
                        system_prompt="Return ONLY raw JSON conforming to the schema. No markdown, no prose.",
                    )
                    analysis = result.data
                    model_name = result.model_name
                    latency_ms = result.latency_ms
                    break
                except Exception as reprompt_err:
                    logger.warning(
                        "Re-prompt for %s (%s) failed: %s. Proceeding to next candidate in router.",
                        provider,
                        candidate_model,
                        reprompt_err,
                    )
                    continue
            except Exception as provider_err:
                logger.warning(
                    "Candidate %s (%s) failed with provider error (%s). Proceeding to next candidate in router without re-prompt.",
                    provider,
                    candidate_model,
                    provider_err,
                )
                continue

    if analysis is None:
        logger.info("All LLM candidates exhausted. Engaging rule-based fallback summary.")
        analysis = build_rule_based_analysis(prs, tickets, recent_flag_ids)
        model_name = "rule_based_fallback"
        latency_ms = 0.0

    logger.info("Final analysis choice: model=%s", model_name)

    # Grounding Check: Normalize and rewrite flag item_id, drop unknown IDs, and ground reasons & evidence
    grounded_flags: List[PriorityFlag] = []
    for flag in analysis.priority_flags:
        canon_id = normalize_llm_id(flag.item_id, valid_canonical_ids)
        if canon_id is None:
            warning_msg = f"Dropped ungrounded priority flag with item_id: {flag.item_id}"
            logger.warning(warning_msg)
            add_state_error(state, warning_msg)
            continue

        # Rewrite flag item_id to canonical ID
        flag.item_id = canon_id
        real_item = item_map.get(canon_id)

        if real_item is not None:
            real_age = real_item.age_days
            real_author = getattr(real_item, "author", None) or getattr(real_item, "assignee", "") or ""

            # Check and correct evidence
            if flag.evidence is None:
                flag.evidence = FlagEvidence(age_days=real_age, author_or_assignee=real_author)
            else:
                age_mismatch = abs(flag.evidence.age_days - real_age) > 1
                author_mismatch = not (
                    flag.evidence.author_or_assignee.strip().lower() in real_author.lower()
                    or real_author.lower() in flag.evidence.author_or_assignee.strip().lower()
                ) if (real_author and flag.evidence.author_or_assignee) else bool(real_author != flag.evidence.author_or_assignee)

                if age_mismatch or author_mismatch:
                    logger.info("Overwriting mismatched evidence for %s with ground truth.", canon_id)
                    flag.evidence.age_days = real_age
                    flag.evidence.author_or_assignee = real_author

            # Build displayed reason from evidence in code without any model numbers
            if isinstance(real_item, PullRequest):
                rev_str = f"reviewers: {', '.join(real_item.reviewers)}" if real_item.reviewers else "no reviewers"
                base_reason = f"Open for {real_item.age_days} days, {rev_str}, last activity {real_item.last_commit_days} days ago"
            else:
                assignee_str = f"assigned to {real_item.assignee}" if real_item.assignee else "unassigned"
                base_reason = f"Open for {real_item.age_days} days, {assignee_str}, last activity {real_item.days_since_update} days ago"

            # Check unsupported claims in reason
            if has_unsupported_claim(flag.reason, real_item):
                logger.info("Unsupported claim found in reason for %s. Replacing with rule-based facts.", canon_id)
                flag.reason = base_reason
            else:
                # Keep LLM text only for non-numeric explanation, removing any digits before display
                cleaned_llm = re.sub(r"\d+", "", flag.reason or "").strip()
                cleaned_llm = re.sub(r"(?i)\b(open for|inactive for|no commits in|last activity|no activity for)\s+days?\b", "", cleaned_llm)
                cleaned_llm = re.sub(r"\s+", " ", cleaned_llm).strip(" .,;:-")
                if cleaned_llm and len(cleaned_llm) > 3 and cleaned_llm.lower() not in base_reason.lower():
                    flag.reason = f"{base_reason}. {cleaned_llm}"
                else:
                    flag.reason = base_reason

            # Check abandoned PR rules (age >= abandoned_pr_days)
            if isinstance(real_item, PullRequest) and real_item.age_days >= settings.abandoned_pr_days:
                flag.urgency = "watch"
                flag.suggested_action = "Ask the author if still active, otherwise close"

        # Deduplicate with recent flags in DB
        if flag.item_id in recent_flag_ids and flag.urgency == "watch":
            logger.debug("Skipping duplicate watch flag: %s", flag.item_id)
            continue

        grounded_flags.append(flag)

    analysis.priority_flags = grounded_flags

    # Grounding Check: Recommendation must name at least one real canonical ID
    rec_cids = extract_canonical_ids_from_text(analysis.one_recommendation, valid_canonical_ids)
    if not rec_cids or not is_text_grounded(analysis.one_recommendation, valid_canonical_ids):
        logger.warning(
            "Recommendation ('%s') did not name any valid canonical ID. Replacing with grounded sentence.",
            analysis.one_recommendation,
        )
        analysis.one_recommendation = generate_stale_fallback_sentence(
            prs, tickets, field_type="recommendation"
        )

    # Grounding Check: Scan velocity_note and patterns using same normalization
    if not is_text_grounded(analysis.velocity_note, valid_canonical_ids):
        logger.warning(
            "Ungrounded ID found in velocity_note ('%s'). Replacing with rule-based sentence.",
            analysis.velocity_note,
        )
        analysis.velocity_note = generate_stale_fallback_sentence(
            prs, tickets, field_type="velocity"
        )

    grounded_patterns: List[str] = []
    for pattern in analysis.patterns:
        if is_text_grounded(pattern, valid_canonical_ids):
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
    return analysis, model_name, latency_ms
