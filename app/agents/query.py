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
from app.llm.schemas import QueryAnswerOutput, QueryFilters, QueryIntent

logger = logging.getLogger(__name__)


class QueryDataCache:
    """In-memory cache for workspace PRs and Jira issues with TTL."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(
        self, workspace_id: str, ttl_seconds: float
    ) -> Optional[Tuple[List[PullRequest], List[JiraIssue], List[str]]]:
        entry = self._cache.get(workspace_id)
        if not entry:
            return None
        elapsed = time.monotonic() - entry["timestamp"]
        if elapsed < ttl_seconds:
            return entry["prs"], entry["issues"], entry["sources_used"]
        # Expired
        self._cache.pop(workspace_id, None)
        return None

    def set(
        self,
        workspace_id: str,
        prs: List[PullRequest],
        issues: List[JiraIssue],
        sources_used: List[str],
    ) -> None:
        self._cache[workspace_id] = {
            "timestamp": time.monotonic(),
            "prs": list(prs),
            "issues": list(issues),
            "sources_used": list(sources_used),
        }

    def clear(self, workspace_id: Optional[str] = None) -> None:
        if workspace_id:
            self._cache.pop(workspace_id, None)
        else:
            self._cache.clear()


_query_cache = QueryDataCache()


def clear_query_cache(workspace_id: Optional[str] = None) -> None:
    """Utility to clear the in-memory query cache."""
    _query_cache.clear(workspace_id)


def is_out_of_scope_by_keywords(question: str) -> bool:
    """Keyword rule: none of pr, pull request, ticket, issue, jira, github, stale, repo, KAN, open, review, assigned means out_of_scope."""
    q_lower = question.lower()
    settings = get_settings()

    keywords = [
        r"\b(pr|prs|pull\s+requests?)\b",
        r"\b(tickets?|issues?)\b",
        r"\b(jira|github|stale)\b",
        r"\b(repos?|repositories|repository)\b",
        r"\bkan\b",
        r"\bkan-\d+\b",
        r"\b(open|opened)\b",
        r"\b(reviews?|reviewers?|reviewing)\b",
        r"\b(assigned|assignees?|unassigned)\b",
    ]
    for pattern in keywords:
        if re.search(pattern, q_lower):
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
    """Classify with keyword and regex rules first for simple questions.

    Rules:
    - none of: pr, pull request, ticket, issue, jira, github, stale, repo, KAN, open, review, assigned -> out_of_scope
    - ID pattern (KAN-123, owner/repo#123) -> lookup
    - oldest, longest, waiting, most stale -> rank
    - how many, count, number of -> count
    - otherwise -> summary / fallback
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

    # Rule 1: ID pattern -> lookup
    extracted_id = extract_lookup_id(question)
    if extracted_id:
        src = "github" if "#" in extracted_id else ("jira" if "-" in extracted_id else "all")
        sources_needed = ["github"] if src == "github" else (["jira"] if src == "jira" else ["github", "jira"])
        return QueryIntent(
            operation="lookup",
            query_type="lookup",
            sources_needed=sources_needed,
            entities=[extracted_id],
            entity_ids=[extracted_id],
            filters=QueryFilters(source=src if src != "all" else None),
            time_range="24h",
        )

    # Determine sources
    has_gh = bool(re.search(r"\b(pr|prs|pull\s+requests?|github|repo)\b", q_lower))
    has_jr = bool(re.search(r"\b(tickets?|issues?|jira|kan)\b", q_lower))
    if has_gh and not has_jr:
        src = "github"
        sources_needed = ["github"]
    elif has_jr and not has_gh:
        src = "jira"
        sources_needed = ["jira"]
    else:
        src = None
        sources_needed = ["github", "jira"]

    stale_only = "stale" in q_lower

    # Rule 2: oldest, longest, waiting, most stale -> rank
    rank_terms = ["oldest", "longest", "waiting", "most stale", "most inactive", "highest age", "slowest"]
    if any(term in q_lower for term in rank_terms):
        metric = "days_since_update" if "inactive" in q_lower else "age_days"
        limit_match = re.search(r"\b(?:top|first)?\s*(\d+)\b", q_lower)
        limit = 5
        if limit_match:
            try:
                parsed_limit = int(limit_match.group(1))
                if 1 <= parsed_limit <= 10:
                    limit = parsed_limit
            except ValueError:
                pass
        return QueryIntent(
            operation="rank",
            metric=metric,
            order="desc",
            limit=limit,
            filters=QueryFilters(source=src, stale_only=stale_only),
            sources_needed=sources_needed,
            query_type="stale" if stale_only else "status",
            time_range="24h",
        )

    # Rule 3: how many, count, number of -> count
    count_terms = ["how many", "count", "number of", "total number", "total count", "total of"]
    if any(term in q_lower for term in count_terms):
        return QueryIntent(
            operation="count",
            metric="none",
            order="desc",
            limit=5,
            filters=QueryFilters(source=src, stale_only=stale_only),
            sources_needed=sources_needed,
            query_type="stale" if stale_only else "status",
            time_range="24h",
        )

    # Rule 4: list / show / which
    list_terms = ["list", "show all", "show me", "which"]
    if any(term in q_lower for term in list_terms):
        return QueryIntent(
            operation="list",
            metric="none",
            order="desc",
            limit=5,
            filters=QueryFilters(source=src, stale_only=stale_only),
            sources_needed=sources_needed,
            query_type="stale" if stale_only else "status",
            time_range="24h",
        )

    # Default fallback
    return QueryIntent(
        operation="summary",
        metric="none",
        order="desc",
        limit=5,
        filters=QueryFilters(source=src, stale_only=stale_only),
        sources_needed=sources_needed,
        query_type="status",
        time_range="24h",
    )


async def classify_query_intent(
    question: str,
    router: Optional[LLMRouter] = None,
) -> Tuple[QueryIntent, str]:
    """Step 1: Classify question using keyword/regex rules first. Only call LLM if rules cannot decide."""
    rule_intent = classify_intent_with_rules(question)
    if rule_intent.operation in ["out_of_scope", "lookup", "rank", "count", "list"]:
        logger.info("Query classified by rules: operation=%s, sources=%s", rule_intent.operation, rule_intent.sources_needed)
        return rule_intent, "rule_based_classifier"

    settings = get_settings()
    llm_router = router or LLMRouter()

    prompt = (
        f"Classify the following software engineering operational question into structured intent:\n\n"
        f"Question: \"{question}\"\n\n"
        f"Identify:\n"
        f"- operation: rank, count, list, lookup, summary, or out_of_scope\n"
        f"- metric: age_days, days_since_update, comment_count, or none\n"
        f"- order: asc or desc\n"
        f"- limit: integer 1-10 (default 5)\n"
        f"- sources_needed: list containing 'github', 'jira', or empty if out_of_scope\n"
        f"- entities: list of specific ticket IDs, repos, or usernames mentioned\n"
        f"- query_type: status, blockers, stale, velocity, unassigned, lookup, or out_of_scope\n"
        f"- time_range: 24h, 7d, or all\n"
    )

    try:
        call_coro = llm_router.route(prompt, schema=QueryIntent, task_type="intent")
        result = await asyncio.wait_for(call_coro, timeout=15.0)
        intent_data = result.data
        if intent_data.query_type == "out_of_scope" and intent_data.operation != "out_of_scope":
            intent_data.operation = "out_of_scope"
        elif intent_data.operation == "out_of_scope" and intent_data.query_type != "out_of_scope":
            intent_data.query_type = "out_of_scope"
        logger.info("Query classified by model '%s': %s", result.model_name, intent_data)
        return intent_data, result.model_name
    except Exception as exc:
        logger.warning("LLM intent classification failed or timed out (%s). Using rule fallback.", exc)
        return rule_intent, "rule_based_classifier"


async def fetch_query_sources(
    sources_needed: List[str],
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
) -> Tuple[List[PullRequest], List[JiraIssue], List[str]]:
    """Step 2: Fetch only the sources the intent needs over the FULL collected data.

    Returns:
        Tuple of (full_prs, full_issues, sources_used).
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
                prs = gh_res  # FULL collected data, no prompt slicing
            else:
                logger.warning("GitHub query fetch failed: %s", gh_res)

        if fetch_jr:
            jr_res = results[idx]
            if isinstance(jr_res, list):
                issues = jr_res  # FULL collected data, no prompt slicing
            else:
                logger.warning("Jira query fetch failed: %s", jr_res)

    if github_client is None and fetch_gh:
        await gh_client.close()
    if jira_client is None and fetch_jr:
        await jr_client.close()

    return prs, issues, sources_used


def build_work_items(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
) -> List[Dict[str, Any]]:
    """Build unified work item representation for Python ranking, counting, and filtering."""
    items: List[Dict[str, Any]] = []
    for pr in prs:
        cid = canonical_id("github", identifier=pr.number, repo=pr.repo)
        items.append({
            "id": cid,
            "title": pr.title,
            "author": pr.author or "unknown",
            "age_days": int(pr.age_days),
            "days_since_update": int(pr.last_commit_days),
            "comment_count": 0,
            "is_stale": bool(pr.is_stale),
            "source": "github",
            "status": "open",
        })
    for t in tickets:
        cid = canonical_id("jira", t.key)
        items.append({
            "id": cid,
            "title": t.title,
            "author": t.assignee or "Unassigned",
            "age_days": int(t.age_days),
            "days_since_update": int(t.days_since_update),
            "comment_count": int(t.comment_count),
            "is_stale": bool(t.is_stale),
            "source": "jira",
            "status": t.status,
        })
    return items


def compute_query_results(
    intent: QueryIntent,
    items: List[Dict[str, Any]],
    question: str,
) -> Tuple[List[Dict[str, Any]], int, str]:
    """Execute computation (rank, count, list, lookup) in Python over full collected data.

    Returns:
        Tuple of (computed_rows, total_count, template_answer).
    """
    filtered = list(items)

    # 1. Filter by source
    src = intent.filters.source
    if not src and intent.sources_needed and len(intent.sources_needed) == 1:
        src = intent.sources_needed[0].lower()

    if src in ["github", "jira"]:
        filtered = [it for it in filtered if it["source"] == src]

    # 2. Filter by stale
    if intent.filters.stale_only:
        filtered = [it for it in filtered if it["is_stale"]]

    # 3. Filter by author
    if intent.filters.author:
        auth_q = intent.filters.author.lower()
        filtered = [it for it in filtered if auth_q in it["author"].lower()]

    # 4. Filter by status
    if intent.filters.status:
        stat_q = intent.filters.status.lower()
        filtered = [it for it in filtered if stat_q in it["status"].lower()]

    total_count = len(filtered)

    # Noun string
    if src == "github":
        noun = "stale pull requests" if intent.filters.stale_only else "open pull requests"
        base_noun = "pull requests"
    elif src == "jira":
        noun = "stale Jira tickets" if intent.filters.stale_only else "open Jira tickets"
        base_noun = "Jira tickets"
    else:
        noun = "stale work items" if intent.filters.stale_only else "open work items"
        base_noun = "work items"

    # Operation: COUNT
    if intent.operation == "count":
        template_answer = f"There are {total_count} {noun}."
        return [], total_count, template_answer

    # Operation: RANK
    if intent.operation == "rank":
        metric = intent.metric if intent.metric in ["age_days", "days_since_update", "comment_count"] else "age_days"
        reverse = (intent.order == "desc")
        filtered.sort(key=lambda x: x[metric], reverse=reverse)
        limit = min(max(intent.limit, 1), 10)
        computed_rows = filtered[:limit]

        if metric == "age_days":
            metric_phrase = "by days since opened"
            unit = "days"
        elif metric == "days_since_update":
            metric_phrase = "by days since last update"
            unit = "days inactive"
        elif metric == "comment_count":
            metric_phrase = "by comment count"
            unit = "comments"
        else:
            metric_phrase = "by days since opened"
            unit = "days"

        if not computed_rows:
            template_answer = f"No {base_noun} found to rank."
        else:
            lines = [f"Top {len(computed_rows)} {base_noun} ranked {metric_phrase} (total: {total_count}):"]
            for i, r in enumerate(computed_rows, 1):
                lines.append(f"{i}. {r['id']}: {r['title']} ({r[metric]} {unit}, author: {r['author']})")
            template_answer = "\n".join(lines)
        return computed_rows, total_count, template_answer

    # Operation: LIST
    if intent.operation == "list":
        limit = min(max(intent.limit, 1), 10)
        computed_rows = filtered[:limit]
        if not computed_rows:
            template_answer = f"No {base_noun} found."
        else:
            lines = [f"Found {total_count} {noun} (showing top {len(computed_rows)}):"]
            for i, r in enumerate(computed_rows, 1):
                lines.append(f"- {r['id']}: {r['title']} ({r['age_days']} days, author: {r['author']})")
            template_answer = "\n".join(lines)
        return computed_rows, total_count, template_answer

    # Operation: LOOKUP
    if intent.operation == "lookup":
        target_id = extract_lookup_id(question, intent.entity_ids or intent.entities)
        if not target_id:
            return [], 0, "No target ID found in question."
        matched = None
        for it in items:
            if it["id"] == target_id or it["id"].upper() == target_id.upper():
                matched = it
                break
            if target_id.startswith("#") and it["id"].endswith(target_id):
                matched = it
                break
        if not matched:
            return [], 0, f"{target_id} is not in the collected data."
        template_answer = f"{matched['id']}: {matched['title']} ({matched['age_days']} days, author: {matched['author']}, status: {matched['status']})."
        return [matched], 1, template_answer

    # Default / Summary
    limit = min(max(intent.limit, 1), 10)
    return filtered[:limit], total_count, ""


def verify_rephrased_answer(
    llm_answer: str,
    computed_rows: List[Dict[str, Any]],
    total_count: int,
    operation: str,
    metric: str,
) -> bool:
    """Verify that every number and ID in the LLM rephrased text exists in computed result."""
    allowed_numbers = {total_count, len(computed_rows)}
    for i in range(1, len(computed_rows) + 2):
        allowed_numbers.add(i)

    for r in computed_rows:
        allowed_numbers.add(r["age_days"])
        allowed_numbers.add(r["days_since_update"])
        allowed_numbers.add(r["comment_count"])
        num_match = re.search(r"#(\d+)", r["id"])
        if num_match:
            allowed_numbers.add(int(num_match.group(1)))
        jira_num_match = re.search(r"-(\d+)", r["id"])
        if jira_num_match:
            allowed_numbers.add(int(jira_num_match.group(1)))

    raw_numbers = re.findall(r"\b\d+\b", llm_answer)
    for num_str in raw_numbers:
        val = int(num_str)
        if val not in allowed_numbers:
            logger.warning("Verification failed: number %d not in allowed computed numbers %s", val, allowed_numbers)
            return False

    allowed_ids = {r["id"].lower() for r in computed_rows}
    for match in JIRA_KEY_REGEX.finditer(llm_answer):
        k = match.group(1).lower()
        if not any(k in aid for aid in allowed_ids):
            logger.warning("Verification failed: ungrounded Jira key in LLM answer: %s", match.group(1))
            return False

    if operation == "rank" and metric == "age_days":
        if "days since opened" not in llm_answer.lower() and "days" not in llm_answer.lower():
            logger.warning("Verification failed: metric not stated in LLM rank answer")
            return False

    return True


def build_query_context(
    prs: List[PullRequest],
    tickets: List[JiraIssue],
) -> Tuple[str, Dict[str, Any], Set[str]]:
    """Build formatted context string with exact canonical IDs and facts for LLM prompts."""
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

    return "\n".join(lines), valid_items, valid_ids


def sanitize_and_ground_answer(
    raw_answer: str,
    valid_ids: Set[str],
    items_map: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """Enforce strict grounding on query answers: remove invented IDs."""
    answer = raw_answer.strip()
    invented_ids_found: Set[str] = set()
    valid_ids_found: Set[str] = set()

    for match in JIRA_KEY_REGEX.finditer(answer):
        found_key = match.group(0).strip()
        canon = normalize_llm_id(found_key, valid_ids)
        if canon and canon in valid_ids:
            valid_ids_found.add(canon)
        else:
            invented_ids_found.add(found_key)

    for match in list(FULL_PR_REGEX.finditer(answer)) + list(PARTIAL_PR_REGEX.finditer(answer)):
        found_pr = match.group(0).strip()
        canon = normalize_llm_id(found_pr, valid_ids)
        if canon and canon in valid_ids:
            valid_ids_found.add(canon)
        else:
            invented_ids_found.add(found_pr)

    for inv_id in invented_ids_found:
        logger.warning("Removing invented item ID from query answer: %s", inv_id)
        answer = re.sub(rf"\b{re.escape(inv_id)}\b", "", answer)

    answer = re.sub(r"\s+", " ", answer).strip(" ,;:-")

    if not answer or (invented_ids_found and not valid_ids_found and len(answer) < 30):
        return "The requested information is not in the collected data.", []

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

    Computation runs in Python over the FULL collected data.
    """
    t_start = time.perf_counter()
    llm_router = router or LLMRouter()
    settings = get_settings()

    # Step 1: Intent Classification (Rules first)
    intent, classifier_model = await classify_query_intent(question, router=llm_router)

    # Fast return for out-of-scope questions: no data fetch, no LLM call
    if intent.operation == "out_of_scope" or intent.query_type == "out_of_scope":
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": "I can only answer questions about your GitHub pull requests and Jira tickets.",
            "sources_used": [],
            "item_ids": [],
            "model_used": classifier_model,
            "latency_ms": round(latency_ms, 2),
            "data": [],
            "total_count": 0,
        }

    # Step 2: Source Collection with workspace cache
    cached_data = _query_cache.get(workspace_id, float(settings.query_cache_ttl_seconds))
    if cached_data is not None:
        prs, tickets, sources_used = cached_data
    else:
        prs, tickets, sources_used = await fetch_query_sources(
            sources_needed=intent.sources_needed,
            github_client=github_client,
            jira_client=jira_client,
        )
        _query_cache.set(workspace_id, prs, tickets, sources_used)

    all_items = build_work_items(prs, tickets)
    valid_ids = {it["id"] for it in all_items}

    # Check for missing target ID in lookup: build answer in code, no LLM call
    target_id = extract_lookup_id(question, intent.entity_ids or intent.entities)
    if target_id and not is_id_in_valid_data(target_id, valid_ids):
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": f"{target_id} is not in the collected data.",
            "sources_used": sources_used,
            "item_ids": [],
            "model_used": "rule_based",
            "latency_ms": round(latency_ms, 2),
            "data": [],
            "total_count": 0,
        }

    if not valid_ids:
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": "The requested information is not in the collected data.",
            "sources_used": sources_used,
            "item_ids": [],
            "model_used": classifier_model,
            "latency_ms": round(latency_ms, 2),
            "data": [],
            "total_count": 0,
        }

    # Step 3: Python computation over FULL data for rank, count, list, lookup
    if intent.operation in ["rank", "count", "list", "lookup"]:
        computed_rows, total_count, template_answer = compute_query_results(
            intent=intent,
            items=all_items,
            question=question,
        )

        final_answer = template_answer
        model_used = "template_code"

        # LLM rephrasing is optional; verify all numbers and IDs against computed result
        if template_answer and "not in the collected data" not in template_answer:
            try:
                candidates = await llm_router.get_candidates(task_type="analysis")
                if candidates:
                    provider, candidate_model = candidates[0]
                    rephrase_prompt = (
                        f"You are TeamPulse, an AI operations agent. Rephrase the following factual answer cleanly and concisely.\n"
                        f"You MUST NOT change or invent any numbers or item IDs. State the metric ('by days since opened').\n\n"
                        f"Original Answer:\n{template_answer}\n"
                    )
                    call_coro = llm_router.call_candidate(
                        provider=provider,
                        model_name=candidate_model,
                        prompt=f"{rephrase_prompt}\nReturn JSON with schema: {{\"answer\": \"string\"}}.",
                        schema=QueryAnswerOutput,
                        task_type="analysis",
                    )
                    # 15 second timeout per LLM call in query path with low thinking level
                    result = await asyncio.wait_for(call_coro, timeout=15.0)
                    llm_text = result.data.answer.strip()
                    if verify_rephrased_answer(llm_text, computed_rows, total_count, intent.operation, intent.metric):
                        final_answer = llm_text
                        model_used = result.model_name
                    else:
                        logger.info("LLM answer failed verification; reverting to template answer.")
                        final_answer = template_answer
                        model_used = "template_verified"
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("LLM rephrase failed or timed out (%s). Using template answer.", exc)
                final_answer = template_answer
                model_used = "template_fallback"

        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "answer": final_answer,
            "sources_used": sources_used,
            "item_ids": [r["id"] for r in computed_rows],
            "model_used": model_used,
            "latency_ms": round(latency_ms, 2),
            "data": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "age_days": r["age_days"],
                    "author": r["author"],
                }
                for r in computed_rows
            ],
            "total_count": total_count,
        }

    # Step 4: Summary / General query (Long-context analysis path capped at prompt max)
    context_str, items_map, _ = build_query_context(
        prs[: settings.max_items_in_prompt], tickets[: settings.max_items_in_prompt]
    )
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
            call_coro = llm_router.call_candidate(
                provider=provider,
                model_name=candidate_model,
                prompt=f"{prompt}\nReturn JSON with schema: {{\"answer\": \"string\"}}.",
                schema=QueryAnswerOutput,
                task_type="analysis",
            )
            result = await asyncio.wait_for(call_coro, timeout=15.0)
            raw_answer = result.data.answer
            model_used = result.model_name
            break
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Candidate %s failed or timed out for query: %s", candidate_model, exc)
            continue

    if not raw_answer:
        matched_items = [
            f"{cid}: {it['title']}"
            for cid, it in ((it["id"], it) for it in all_items)
            if any(term in str(it).lower() for term in question.lower().split() if len(term) > 3)
        ]
        if matched_items:
            raw_answer = "Found matching items: " + "; ".join(matched_items[:3])
        else:
            raw_answer = "The requested information is not in the collected data."
        model_used = "rule_based_fallback"

    clean_answer, valid_mentioned_ids = sanitize_and_ground_answer(
        raw_answer, valid_ids, {it["id"]: it for it in all_items}
    )
    latency_ms = (time.perf_counter() - t_start) * 1000

    top_computed = all_items[:10]
    return {
        "answer": clean_answer,
        "sources_used": sources_used,
        "item_ids": valid_mentioned_ids,
        "model_used": model_used,
        "latency_ms": round(latency_ms, 2),
        "data": [
            {
                "id": r["id"],
                "title": r["title"],
                "age_days": r["age_days"],
                "author": r["author"],
            }
            for r in top_computed
        ],
        "total_count": len(all_items),
    }
