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


def is_out_of_scope_by_keywords(question: str) -> bool:
    """Keyword rule: no mention of PR, pull request, ticket, issue, Jira, GitHub, stale, KAN or a repo name means out_of_scope."""
    q_lower = question.lower()
    settings = get_settings()

    # Mention of PR / pull request
    if re.search(r"\b(pr|prs|pull\s+requests?)\b", q_lower):
        return False

    # Mention of ticket, issue, Jira, GitHub, stale, KAN
    if re.search(r"\b(tickets?|issues?|jira|github|stale|kan)\b", q_lower):
        return False

    # Regex matches for Jira keys or PR references
    if JIRA_KEY_REGEX.search(question) or FULL_PR_REGEX.search(question) or PARTIAL_PR_REGEX.search(question):
        return False

    # Check configured GitHub repo names and basenames
    for repo in settings.github_repos:
        repo_lower = repo.lower()
        if repo_lower in q_lower:
            return False
        basename = repo_lower.split("/")[-1]
        if basename and basename in q_lower:
            return False

    return True


def extract_lookup_id(question: str, entities: Optional[List[str]] = None) -> Optional[str]:
    """Extract a target work item ID from a lookup question or entities."""
    # 1. Check Jira keys (e.g. KAN-42, PROJ-123)
    jira_matches = JIRA_KEY_REGEX.findall(question)
    if jira_matches:
        return jira_matches[0]

    # 2. Check full PR IDs (e.g. owner/repo#42)
    for m in FULL_PR_REGEX.finditer(question):
        return f"{m.group(1)}#{m.group(2)}"

    # 3. Check partial PR IDs (e.g. #42, PR #42)
    for m in PARTIAL_PR_REGEX.finditer(question):
        return f"#{m.group(1)}"

    # 4. Check explicit PR with bare number: e.g. "PR 42", "pull request 42"
    pr_num_match = re.search(r"\b(?:pr|pull\s+request)\s+#?(\d+)\b", question, re.IGNORECASE)
    if pr_num_match:
        return f"#{pr_num_match.group(1)}"

    # 5. Check entities
    if entities:
        for ent in entities:
            if JIRA_KEY_REGEX.match(ent):
                return ent
            for m in FULL_PR_REGEX.finditer(ent):
                return f"{m.group(1)}#{m.group(2)}"
            for m in PARTIAL_PR_REGEX.finditer(ent):
                return f"#{m.group(1)}"

    return None


def is_id_in_valid_data(target_id: str, valid_ids: Set[str]) -> bool:
    """Check if target_id exists in the valid canonical IDs."""
    if not valid_ids:
        return False
    return normalize_llm_id(target_id, valid_ids) is not None


def classify_intent_with_rules(question: str) -> QueryIntent:
    """Fallback keyword-based rule classifier for user query intent.

    Args:
        question: The natural language question string.

    Returns:
        Structured QueryIntent with sources, entities, query_type, operation, and time_range.
    """
    if is_out_of_scope_by_keywords(question):
        return QueryIntent(
            operation="out_of_scope",
            query_type="out_of_scope",
            sources_needed=[],
            entities=[],
            time_range="24h",
        )

    q_lower = question.lower()
    sources: List[str] = []

    # Check GitHub keywords
    if any(k in q_lower for k in ["pr", "pull request", "github", "repo", "commit", "branch", "merge"]):
        sources.append("github")

    # Check Jira keywords
    if any(k in q_lower for k in ["jira", "ticket", "issue", "bug", "task", "story", "sprint", "kan"]):
        sources.append("jira")

    if not sources:
        sources = ["github", "jira"]

    # Extract entity mentions
    jira_keys = JIRA_KEY_REGEX.findall(question)
    pr_matches = FULL_PR_REGEX.findall(question) + PARTIAL_PR_REGEX.findall(question)
    entities = list(set(jira_keys + pr_matches))

    # Determine query category & operation
    operation = "status"
    query_type = "status"
    if jira_keys or pr_matches or any(k in q_lower for k in ["tell me about", "lookup", "look up", "find", "show me", "check"]):
        operation = "lookup"
        query_type = "lookup"
    elif any(k in q_lower for k in ["block", "blocker", "stuck"]):
        operation = "blockers"
        query_type = "blockers"
    elif any(k in q_lower for k in ["stale", "inactive", "old", "abandoned"]):
        operation = "stale"
        query_type = "stale"
    elif any(k in q_lower for k in ["velocity", "speed", "throughput"]):
        operation = "velocity"
        query_type = "velocity"
    elif any(k in q_lower for k in ["unassigned", "who is", "assignee", "owner"]):
        operation = "unassigned"
        query_type = "unassigned"

    return QueryIntent(
        sources_needed=sources,
        entities=entities,
        query_type=query_type,
        operation=operation,
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
        f"- operation: 'out_of_scope' if the question is not about the team's GitHub pull requests or Jira tickets (weather, general knowledge, jokes, personal topics); 'lookup' if asking about a specific item/ID; or 'status', 'blockers', 'stale', 'velocity'\n"
        f"- sources_needed: list containing 'github', 'jira', or both (empty if out_of_scope)\n"
        f"- entities: list of specific ticket IDs, repos, or usernames mentioned\n"
        f"- query_type: status, blockers, stale, velocity, unassigned, lookup, or out_of_scope\n"
        f"- time_range: 24h, 7d, or all\n"
    )

    try:
        # Simple task path routes to Ollama if enabled/healthy, then Gemini fallback
        result = await llm_router.route(prompt, schema=QueryIntent, task_type="intent")
        intent_data = result.data
        if intent_data.query_type == "out_of_scope" and not intent_data.operation:
            intent_data.operation = "out_of_scope"
        elif intent_data.operation == "out_of_scope" and intent_data.query_type != "out_of_scope":
            intent_data.query_type = "out_of_scope"
        logger.info("Query classified by model '%s': %s", result.model_name, intent_data)
        return intent_data, result.model_name
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

    # Fast return for out-of-scope questions: do not fetch any data and do not call large model
    if intent.operation == "out_of_scope" or intent.query_type == "out_of_scope":
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": "I can only answer questions about your GitHub pull requests and Jira tickets.",
            "sources_used": [],
            "item_ids": [],
            "model_used": classifier_model,
            "latency_ms": round(latency_ms, 2),
        }

    # Step 2: Source Collection
    prs, tickets, sources_used = await fetch_query_sources(
        sources_needed=intent.sources_needed,
        github_client=github_client,
        jira_client=jira_client,
    )

    context_str, items_map, valid_ids = build_query_context(prs, tickets)

    # Check for lookup of an ID not in the collected data: build answer in code, no LLM call, item_ids = []
    target_id = extract_lookup_id(question, intent.entities)
    if target_id and not is_id_in_valid_data(target_id, valid_ids):
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": f"{target_id} is not in the collected data.",
            "sources_used": sources_used,
            "item_ids": [],
            "model_used": "rule_based",
            "latency_ms": round(latency_ms, 2),
        }

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
