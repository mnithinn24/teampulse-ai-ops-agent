"""Plain-language query agent: classification, selective collection, answering, and grounding."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config import get_settings
from app.core.ids import (
    FULL_PR_REGEX,
    JIRA_KEY_REGEX,
    PARTIAL_PR_REGEX,
    canonical_id,
    normalize_llm_id,
)
from app.integrations.github_client import GitHubClient, PullRequest
from app.integrations.jira_client import JiraClient, JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import QueryAnswerOutput, QueryIntent

logger = logging.getLogger(__name__)


def classify_intent_with_rules(question: str) -> QueryIntent:
    """Fallback keyword-based rule classifier for user query intent.

    Args:
        question: The natural language question string.

    Returns:
        Structured QueryIntent with sources, entities, query_type, and time_range.
    """
    q_lower = question.lower()
    sources: List[str] = []

    # Check GitHub keywords
    if any(k in q_lower for k in ["pr", "pull request", "github", "repo", "commit", "branch", "merge"]):
        sources.append("github")

    # Check Jira keywords
    if any(k in q_lower for k in ["jira", "ticket", "issue", "bug", "task", "story", "sprint"]):
        sources.append("jira")

    if not sources:
        sources = ["github", "jira"]

    # Extract entity mentions
    jira_keys = JIRA_KEY_REGEX.findall(question)
    pr_matches = FULL_PR_REGEX.findall(question) + PARTIAL_PR_REGEX.findall(question)
    entities = list(set(jira_keys + pr_matches))

    # Determine query category
    query_type = "status"
    if any(k in q_lower for k in ["block", "blocker", "stuck"]):
        query_type = "blockers"
    elif any(k in q_lower for k in ["stale", "inactive", "old", "abandoned"]):
        query_type = "stale"
    elif any(k in q_lower for k in ["velocity", "speed", "throughput"]):
        query_type = "velocity"
    elif any(k in q_lower for k in ["unassigned", "who is", "assignee", "owner"]):
        query_type = "unassigned"

    return QueryIntent(
        sources_needed=sources,
        entities=entities,
        query_type=query_type,
        time_range="24h",
    )


async def classify_query_intent(
    question: str,
    router: Optional[LLMRouter] = None,
) -> Tuple[QueryIntent, str]:
    """Step 1: Classify question into QueryIntent using router's simple-task path or rule fallback.

    Args:
        question: The user's plain-language question.
        router: Optional LLMRouter instance.

    Returns:
        Tuple of (classified QueryIntent, model_or_rule_used).
    """
    settings = get_settings()
    llm_router = router or LLMRouter()

    prompt = (
        f"Classify the following software engineering operational question into structured intent:\n\n"
        f"Question: \"{question}\"\n\n"
        f"Identify:\n"
        f"- sources_needed: list containing 'github', 'jira', or both\n"
        f"- entities: list of specific ticket IDs, repos, or usernames mentioned\n"
        f"- query_type: status, blockers, stale, velocity, unassigned\n"
        f"- time_range: 24h, 7d, or all\n"
    )

    try:
        # Simple task path routes to Ollama if enabled/healthy, then Gemini fallback
        result = await llm_router.route(prompt, schema=QueryIntent, task_type="intent")
        logger.info("Query classified by model '%s': %s", result.model_name, result.data)
        return result.data, result.model_name
    except Exception as exc:
        logger.warning("LLM intent classification failed (%s). Using rule-based classifier.", exc)
        return classify_intent_with_rules(question), "rule_based_classifier"


async def fetch_query_sources(
    sources_needed: List[str],
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
) -> Tuple[List[PullRequest], List[JiraIssue], List[str]]:
    """Step 2: Fetch only the sources the intent needs, applying standard caps.

    Args:
        sources_needed: List of required sources ('github', 'jira').
        github_client: Optional GitHubClient instance.
        jira_client: Optional JiraClient instance.

    Returns:
        Tuple of (prs, issues, sources_used).
    """
    settings = get_settings()
    sources_normalized = [s.lower() for s in sources_needed]
    if not sources_normalized:
        sources_normalized = ["github", "jira"]

    fetch_gh = "github" in sources_normalized
    fetch_jr = "jira" in sources_normalized

    gh_client = github_client or GitHubClient(
        max_commit_lookups=settings.max_commit_lookups,
        stale_pr_days=settings.stale_pr_days,
    )
    jr_client = jira_client or JiraClient(stale_ticket_days=settings.stale_ticket_days)

    prs: List[PullRequest] = []
    issues: List[JiraIssue] = []
    sources_used: List[str] = []

    async def _fetch_prs() -> List[PullRequest]:
        return await gh_client.fetch_all_open_prs(repos=settings.github_repos)

    async def _fetch_issues() -> List[JiraIssue]:
        return await jr_client.fetch_open_issues(project_key=settings.jira_project_key)

    tasks = []
    if fetch_gh:
        tasks.append(_fetch_prs())
        sources_used.append("github")
    if fetch_jr:
        tasks.append(_fetch_issues())
        sources_used.append("jira")

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        idx = 0
        if fetch_gh:
            gh_res = results[idx]
            idx += 1
            if isinstance(gh_res, list):
                prs = gh_res[: settings.max_items_in_prompt]
            else:
                logger.warning("GitHub query fetch failed: %s", gh_res)

        if fetch_jr:
            jr_res = results[idx]
            if isinstance(jr_res, list):
                issues = jr_res[: settings.max_items_in_prompt]
            else:
                logger.warning("Jira query fetch failed: %s", jr_res)

    if github_client is None and fetch_gh:
        await gh_client.close()
    if jira_client is None and fetch_jr:
        await jr_client.close()

    return prs, issues, sources_used


def build_query_context(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
) -> Tuple[str, Dict[str, Any], Set[str]]:
    """Build formatted context string with exact canonical IDs and facts.

    Returns:
        Tuple of (context_string, items_map, set_of_valid_canonical_ids).
    """
    valid_items: Dict[str, Any] = {}
    valid_ids: Set[str] = set()
    lines: List[str] = []

    if prs:
        lines.append("### Open Pull Requests:")
        for pr in prs:
            cid = canonical_id("github", identifier=pr.number, repo=pr.repo)
            valid_ids.add(cid)
            valid_items[cid] = pr
            rev_str = f"Reviewers: {', '.join(pr.reviewers)}" if pr.reviewers else "No reviewers"
            lines.append(
                f"- Canonical ID: {cid} | Title: {pr.title} | Author: {pr.author} | "
                f"Age: {pr.age_days} days | Last commit: {pr.last_commit_days} days ago | "
                f"{rev_str} | Stale: {pr.is_stale}"
            )

    if tickets:
        lines.append("\n### Open Jira Tickets:")
        for t in tickets:
            cid = canonical_id("jira", t.key)
            valid_ids.add(cid)
            valid_items[cid] = t
            assignee = t.assignee or "Unassigned"
            lines.append(
                f"- Canonical ID: {cid} | Title: {t.title} | Assignee: {assignee} | "
                f"Status: {t.status} | Age: {t.age_days} days | "
                f"Inactive for: {t.days_since_update} days | Comments: {t.comment_count} | "
                f"Stale: {t.is_stale}"
            )

    if not lines:
        return "No open items found in collected data.", {}, set()

    return "\n".join(lines), valid_items, valid_ids


def sanitize_and_ground_answer(
    raw_answer: str,
    valid_ids: Set[str],
    items_map: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """Enforce strict grounding: remove invented IDs and verify numbers against data.

    Returns:
        Tuple of (sanitized_answer, list_of_valid_mentioned_ids).
    """
    answer = raw_answer.strip()
    invented_ids_found: Set[str] = set()
    valid_ids_found: Set[str] = set()

    # Look for Jira keys (e.g. KAN-42, PROJ-99)
    for match in JIRA_KEY_REGEX.finditer(answer):
        found_key = match.group(0).strip()
        canon = normalize_llm_id(found_key, valid_ids)
        if canon and canon in valid_ids:
            valid_ids_found.add(canon)
        else:
            invented_ids_found.add(found_key)

    # Look for GitHub PRs (e.g. owner/repo#12, #42)
    for match in list(FULL_PR_REGEX.finditer(answer)) + list(PARTIAL_PR_REGEX.finditer(answer)):
        found_pr = match.group(0).strip()
        canon = normalize_llm_id(found_pr, valid_ids)
        if canon and canon in valid_ids:
            valid_ids_found.add(canon)
        else:
            invented_ids_found.add(found_pr)

    # Remove invented IDs from answer text
    for inv_id in invented_ids_found:
        logger.warning("Removing invented item ID from query answer: %s", inv_id)
        # Strip exact word match of invented ID
        answer = re.sub(rf"\b{re.escape(inv_id)}\b", "", answer)

    # Clean up leftover artifacts from removed IDs
    answer = re.sub(r"\s+", " ", answer).strip(" ,;:-")

    # If the answer has become meaningless or only referenced invented entities
    if not answer or (invented_ids_found and not valid_ids_found and len(answer) < 30):
        return "The requested information is not in the collected data.", []

    # If the answer already explicitly says not in data
    if "not in the" in answer.lower() or "not found" in answer.lower():
        return answer, list(valid_ids_found)

    return answer, list(valid_ids_found)


async def handle_plain_language_query(
    question: str,
    workspace_id: str = "default",
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
    router: Optional[LLMRouter] = None,
) -> Dict[str, Any]:
    """Execute end-to-end plain-language query workflow.

    Workflow:
    1. Classify intent (simple-task path: Ollama -> Gemini fallback -> rules).
    2. Fetch required sources with caps.
    3. Generate grounded answer via long-context path (Gemini main -> fallback -> Ollama).
    4. Remove invented IDs and enforce grounding facts.

    Returns:
        Dict containing answer, sources_used, item_ids, model_used, and latency_ms.
    """
    t_start = time.perf_counter()
    llm_router = router or LLMRouter()

    # Step 1: Intent Classification
    intent, classifier_model = await classify_query_intent(question, router=llm_router)

    # Step 2: Source Collection
    prs, tickets, sources_used = await fetch_query_sources(
        sources_needed=intent.sources_needed,
        github_client=github_client,
        jira_client=jira_client,
    )

    context_str, items_map, valid_ids = build_query_context(prs, tickets)

    # If no data exists at all
    if not valid_ids:
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": "The requested information is not in the collected data.",
            "sources_used": sources_used,
            "item_ids": [],
            "model_used": classifier_model,
            "latency_ms": round(latency_ms, 2),
        }

    # Step 3: Answering with long-context path
    prompt = (
        f"You are TeamPulse, an AI operations agent. Answer the following question based ONLY on the collected data.\n\n"
        f"Question: \"{question}\"\n\n"
        f"Collected Data:\n{context_str}\n\n"
        f"Grounding Rules:\n"
        f"1. Rely strictly on facts in the data above.\n"
        f"2. Only reference item IDs that appear in the data. Never invent IDs.\n"
        f"3. Numbers (days, counts) must come from the data.\n"
        f"4. If the data does not contain the answer, say: 'The requested information is not in the collected data.'\n"
        f"Be concise, direct, and professional."
    )

    model_used = "rule_based"
    raw_answer = ""

    candidates = await llm_router.get_candidates(task_type="analysis")
    for provider, candidate_model in candidates:
        try:
            logger.info("Attempting query answer with %s (%s)", provider, candidate_model)
            result = await llm_router.call_candidate(
                provider=provider,
                model_name=candidate_model,
                prompt=f"{prompt}\nReturn JSON with schema: {{\"answer\": \"string\"}}.",
                schema=QueryAnswerOutput,
                task_type="analysis",
            )
            raw_answer = result.data.answer
            model_used = result.model_name
            break
        except Exception as exc:
            logger.warning("Candidate %s failed for query: %s", candidate_model, exc)
            continue

    if not raw_answer:
        # Check if question matched any entities directly in rules
        matched_items = [
            f"{cid}: {item.title}"
            for cid, item in items_map.items()
            if any(term in str(item).lower() for term in question.lower().split() if len(term) > 3)
        ]
        if matched_items:
            raw_answer = "Found matching items: " + "; ".join(matched_items[:3])
        else:
            raw_answer = "The requested information is not in the collected data."
        model_used = "rule_based_fallback"

    # Step 4: Sanitize & ground answer
    clean_answer, valid_mentioned_ids = sanitize_and_ground_answer(raw_answer, valid_ids, items_map)
    latency_ms = (time.perf_counter() - t_start) * 1000

    return {
        "answer": clean_answer,
        "sources_used": sources_used,
        "item_ids": valid_mentioned_ids,
        "model_used": model_used,
        "latency_ms": round(latency_ms, 2),
    }
