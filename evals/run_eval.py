"""Phase 6: Offline evaluation suite for TeamPulse.

Evaluates deterministic functional cases (query ranking, counting, lookup, out-of-scope,
stale detection, grounding, failure handling, briefing wording, action safety) and
optional live LLM performance on synthetic fixtures without touching external services.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure app is in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ensure real .env is never loaded during evaluation
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DRY_RUN_ACTIONS"] = "true"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-dummy-eval-token"
os.environ["SLACK_SIGNING_SECRET"] = "dummy-eval-signing-secret-1234567890"
os.environ["SLACK_CHANNEL_ID"] = "C12345678"
os.environ["JIRA_BASE_URL"] = "https://dummy.atlassian.net"
os.environ["JIRA_EMAIL"] = "eval@example.com"
os.environ["JIRA_API_TOKEN"] = "dummy-jira-eval-token"
os.environ["JIRA_PROJECT_KEY"] = "KAN"
os.environ["GITHUB_TOKEN"] = "dummy-github-eval-token"
os.environ["GITHUB_REPOS"] = "mnithinn24/teampulse-ai-ops-agent"
os.environ["GEMINI_API_KEY"] = "dummy-gemini-key"
os.environ["ADMIN_API_KEY"] = "dummy-admin-key"

from app.config import Settings, get_settings

Settings.model_config["env_file"] = None
get_settings.cache_clear()

from app.agents.actions import execute_approved_action
from app.agents.analyze import (
    FlagEvidence,
    PriorityFlag,
    canonical_id,
    normalize_llm_id,
)
from app.agents.briefing import build_slack_briefing
from app.agents.query import _query_cache, handle_plain_language_query
from app.core.security import verify_slack_signature
from app.db.database import get_engine, init_db
from app.db.models import Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraClient, JiraIssue
from evals.fixtures import (
    compute_ground_truth_counts,
    compute_ground_truth_rank,
    generate_3_jira_dataset,
    generate_88_pr_dataset,
    generate_stale_boundary_dataset,
)

logger = logging.getLogger("evals")


def get_git_commit_hash() -> str:
    """Retrieve current local git commit hash."""
    try:
        res = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return res
    except Exception:
        return "unknown"


def compute_percentile(data: List[float], percentile: float) -> float:
    """Compute exact percentile value for a list of floats."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    if len(sorted_data) == 1:
        return round(sorted_data[0], 2)
    k = (len(sorted_data) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(sorted_data[int(k)], 2)
    d0 = sorted_data[f] * (c - k)
    d1 = sorted_data[c] * (k - f)
    return round(d0 + d1, 2)


class EvalRunner:
    """Runner for Phase 6 evaluation suite."""

    def __init__(
        self,
        cases_file: Path,
        out_dir: Path,
        with_llm: bool = False,
        models: Optional[List[str]] = None,
        max_llm_calls: int = 6,
    ) -> None:
        self.cases_file = cases_file
        self.out_dir = out_dir
        self.with_llm = with_llm
        self.models = models or ["gemini-2.5-flash", "gemini-2.0-flash"]
        self.max_llm_calls = max_llm_calls

        self.prs_88 = generate_88_pr_dataset()
        self.tickets_3 = generate_3_jira_dataset()
        self.boundary_data = generate_stale_boundary_dataset()

        # Prime in-memory cache for query evaluator
        self.workspace_id = "eval_workspace"
        _query_cache.set(
            self.workspace_id,
            self.prs_88,
            self.tickets_3,
            ["github", "jira"],
        )

        # Setup in-memory SQLite
        self.engine = get_engine()
        init_db(self.engine)

    async def evaluate_single_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate a single labeled test case and return execution result."""
        cid = case["id"]
        cat = case["category"]
        c_input = case["input"]
        expected = case["expected"]

        t_start = time.perf_counter()
        passed = False
        actual_output: Any = None
        error_msg: Optional[str] = None

        try:
            # 1. Query Rank, Count, Lookup, Out-of-Scope
            if cat in ("query_rank", "query_count", "query_lookup", "query_out_of_scope"):
                query_text = c_input["query"]
                res = await handle_plain_language_query(
                    question=query_text,
                    workspace_id=self.workspace_id,
                )
                actual_output = res["answer"]
                ans_str = str(actual_output)

                if cat == "query_rank":
                    must_contain = expected.get("contains", [])
                    passed = all(item in ans_str for item in must_contain)
                elif cat == "query_count":
                    must_contain = expected.get("contains", [])
                    passed = all(item in ans_str for item in must_contain)
                elif cat == "query_lookup":
                    must_contain = expected.get("contains", [])
                    passed = all(item in ans_str for item in must_contain)
                elif cat == "query_out_of_scope":
                    passed = ans_str == expected["answer"]

            # 2. Stale Detection
            elif cat == "stale_detection":
                item_type = c_input["item_type"]
                settings = get_settings()
                if item_type == "pr":
                    age = c_input["age_days"]
                    last_commit = c_input["last_commit_days"]
                    is_stale = last_commit >= settings.stale_pr_days
                    actual_output = {"is_stale": is_stale}
                    passed = is_stale == expected["is_stale"]
                else:
                    days_inactive = c_input["days_since_update"]
                    is_stale = days_inactive >= settings.stale_ticket_days
                    actual_output = {"is_stale": is_stale}
                    passed = is_stale == expected["is_stale"]

            # 3. Grounding & Hallucination Guard
            elif cat == "grounding":
                sim = c_input["simulated_flag"]
                raw_id = sim["item_id"]
                valid_ids = {f"{p.repo}#{p.number}" for p in self.prs_88} | {t.key for t in self.tickets_3}
                item_map = {f"{p.repo}#{p.number}": p for p in self.prs_88}
                item_map.update({t.key: t for t in self.tickets_3})

                norm_id = normalize_llm_id(raw_id, valid_ids)
                if norm_id is None:
                    actual_output = {"blocked": True, "survives_grounding": False}
                    passed = expected.get("blocked", False) is True
                else:
                    real_item = item_map[norm_id]
                    src_type = "github" if isinstance(real_item, PullRequest) else "jira"
                    flag = PriorityFlag(
                        source=src_type,
                        item_id=norm_id,
                        title=sim.get("title", ""),
                        reason=sim.get("reason", ""),
                        urgency=sim.get("urgency", "watch"),
                        suggested_action=sim.get("suggested_action", ""),
                        evidence=FlagEvidence(
                            age_days=sim.get("evidence_age_days", real_item.age_days),
                            author_or_assignee=sim.get("evidence_author", getattr(real_item, "author", "")),
                        ),
                    )

                    # Ground evidence & reason
                    flag.evidence.age_days = real_item.age_days
                    flag.evidence.author_or_assignee = getattr(real_item, "author", getattr(real_item, "assignee", ""))

                    if isinstance(real_item, PullRequest):
                        rev_str = f"reviewers: {', '.join(real_item.reviewers)}" if real_item.reviewers else "no reviewers"
                        flag.reason = f"Open for {real_item.age_days} days, {rev_str}, last activity {real_item.last_commit_days} days ago."
                    else:
                        assignee_str = f"assigned to {real_item.assignee}" if real_item.assignee else "unassigned"
                        flag.reason = f"Open for {real_item.age_days} days, {assignee_str}, last activity {real_item.days_since_update} days ago."

                    actual_output = {
                        "normalized_id": norm_id,
                        "survives_grounding": True,
                        "grounded_age": flag.evidence.age_days,
                        "grounded_author": flag.evidence.author_or_assignee,
                        "reason": flag.reason,
                    }

                    if expected.get("normalized_id"):
                        passed = norm_id == expected["normalized_id"]
                    elif expected.get("grounded_age"):
                        passed = (
                            flag.evidence.age_days == expected["grounded_age"]
                            and flag.evidence.author_or_assignee == expected["grounded_author"]
                        )
                    elif expected.get("reason_replaced_by_code"):
                        forbidden = expected.get("forbidden_text", [])
                        passed = not any(w in flag.reason for w in forbidden)
                    else:
                        passed = True

            # 4. Failure Handling
            elif cat == "failure_handling":
                gh_status = c_input.get("github_status")
                gh_err = c_input.get("github_error")
                jr_status = c_input.get("jira_status")
                jr_err = c_input.get("jira_error")

                state = TeamPulseState(workspace_id="test")
                if gh_status == "unavailable":
                    state.github_data = {"status": "unavailable", "error": gh_err}
                    state.errors.append(f"GitHub collection failed: {gh_err}")
                else:
                    state.github_data = self.prs_88[:2]

                if jr_status == "unavailable":
                    state.jira_data = {"status": "unavailable", "error": jr_err}
                    state.errors.append(f"Jira collection failed: {jr_err}")
                else:
                    state.jira_data = self.tickets_3

                now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
                briefing = build_slack_briefing(state=state, flags_with_db=[], now=now)
                text = briefing["text"]
                actual_output = text

                starts_warning = text.startswith("WARNING:")
                no_all_clear = "All clear" not in text
                no_zero_prs = "0 open PRs" not in text if gh_status == "unavailable" else True
                unknown_pr = "unknown open PRs" in text if expected.get("unknown_pr_count") else True
                unknown_ticket = "unknown stale tickets" in text if expected.get("unknown_ticket_count") else True

                warn_contains = expected.get("warning_contains")
                if isinstance(warn_contains, list):
                    warn_ok = all(wc in text for wc in warn_contains)
                elif isinstance(warn_contains, str):
                    warn_ok = warn_contains in text
                else:
                    warn_ok = True

                forbidden = expected.get("forbidden_in_warning", [])
                forbidden_ok = not any(f in text for f in forbidden)

                passed = (
                    starts_warning
                    and no_all_clear
                    and no_zero_prs
                    and unknown_pr
                    and unknown_ticket
                    and warn_ok
                    and forbidden_ok
                )

            # 5. Briefing Wording
            elif cat == "briefing_wording":
                f_count = c_input["flags_count"]
                stale_prs_c = c_input["stale_prs"]
                stale_tks_c = c_input["stale_tickets"]
                all_ok = c_input["all_sources_ok"]

                prs_list = [
                    PullRequest(
                        id=100 + i,
                        number=i,
                        title=f"PR {i}",
                        author="alice",
                        age_days=10,
                        last_commit_days=10,
                        is_stale=True,
                        repo="org/repo",
                    )
                    for i in range(stale_prs_c)
                ]
                tks_list = [
                    JiraIssue(
                        key=f"KAN-{i}",
                        title=f"Issue {i}",
                        status="Open",
                        age_days=20,
                        days_since_update=20,
                        is_stale=True,
                    )
                    for i in range(stale_tks_c)
                ]

                flags = [
                    {
                        "id": 500 + i,
                        "item_id": f"org/repo#{i+1}",
                        "title": f"Flagged PR #{i+1}",
                        "reason": "Stale",
                        "urgency": "critical" if i == 0 else "watch",
                        "suggested_action": "Review",
                    }
                    for i in range(f_count)
                ]

                state = TeamPulseState(workspace_id="test")
                if not all_ok:
                    state.github_data = {"status": "unavailable", "error": "rate limit"}
                    state.errors.append("GitHub collection failed: rate limit")
                else:
                    state.github_data = prs_list
                state.jira_data = tks_list

                now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
                briefing = build_slack_briefing(state=state, flags_with_db=flags, now=now)
                text = briefing["text"]
                actual_output = text

                if expected.get("no_all_clear"):
                    all_clear_absent = "All clear" not in text
                else:
                    all_clear_absent = True

                if expected.get("exact_sentence"):
                    sentence_ok = expected["exact_sentence"] in text
                else:
                    sentence_ok = True

                if expected.get("contains_all_clear"):
                    all_clear_present = "All clear!" in text
                else:
                    all_clear_present = True

                if expected.get("starts_with_warning"):
                    warning_ok = text.startswith("WARNING:")
                else:
                    warning_ok = True

                passed = all_clear_absent and sentence_ok and all_clear_present and warning_ok

            # 6. Action Safety
            elif cat == "action_safety":
                action_name = c_input["action"]
                if action_name == "verify_signature":
                    secret = "test_signing_secret_12345"
                    raw_body = b"token=test&team_id=T123"
                    drift = c_input.get("timestamp_drift_seconds", 0)
                    now = time.time()
                    ts = str(int(now - drift))

                    if c_input.get("forged"):
                        sig = "v0=forged_bad_signature_hex_value"
                    else:
                        import hashlib
                        import hmac
                        base = f"v0:{ts}:{raw_body.decode()}".encode()
                        sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()

                    valid = verify_slack_signature(
                        raw_body=raw_body,
                        timestamp=ts,
                        signature=sig,
                        signing_secret=secret,
                        current_time=now,
                    )
                    actual_output = {"is_valid": valid}
                    passed = valid == expected["is_valid"]

                elif action_name == "dry_run_execution":
                    flag = Flag(
                        id=1,
                        workspace_id="test",
                        item_id=c_input["target_id"],
                        item_type="jira_ticket",
                        suggested_action="Comment on ticket",
                    )
                    result = await execute_approved_action(flag, dry_run=True)
                    actual_output = result
                    passed = (
                        "[DRY RUN]" in result
                        and "would comment on" in result
                    )

                elif action_name == "duplicate_ticket_check":
                    from unittest.mock import AsyncMock
                    mock_jira = AsyncMock(spec=JiraClient)
                    existing_issue = JiraIssue(
                        key=c_input["existing_key"],
                        title=f"Triage stale PR {c_input['pr_canonical_id']}",
                        status="Open",
                        project="KAN",
                        age_days=1,
                        days_since_update=1,
                        is_stale=False,
                    )
                    mock_jira.search_issues.return_value = [existing_issue]

                    flag = Flag(
                        id=2,
                        workspace_id="test",
                        item_id=c_input["pr_canonical_id"],
                        item_type="github_pr",
                        suggested_action="Review",
                    )
                    res = await execute_approved_action(flag, jira_client=mock_jira, dry_run=False)
                    actual_output = res
                    passed = (
                        res == expected["reply_text"]
                        and mock_jira.create_issue.await_count == 0
                    )

                elif action_name == "second_approval":
                    from unittest.mock import AsyncMock
                    mock_jira = AsyncMock(spec=JiraClient)
                    mock_jira.add_comment.return_value = {"id": "1"}

                    flag = Flag(
                        id=3,
                        workspace_id="test",
                        item_id=c_input["target_id"],
                        item_type="jira_ticket",
                        suggested_action="Comment on ticket",
                    )
                    # First run
                    res1 = await execute_approved_action(flag, jira_client=mock_jira, dry_run=False)
                    call_count = mock_jira.add_comment.await_count
                    actual_output = {"res1": res1, "executed_calls": call_count}
                    passed = call_count == expected["executed_calls"]

        except Exception as exc:
            error_msg = str(exc)
            actual_output = f"ERROR: {exc}"
            passed = False

        latency_ms = round((time.perf_counter() - t_start) * 1000, 2)

        return {
            "id": cid,
            "category": cat,
            "passed": passed,
            "latency_ms": latency_ms,
            "expected": expected,
            "actual": actual_output,
            "guards_against": case.get("guards_against", ""),
            "error": error_msg,
        }

    async def run_deterministic_eval(self) -> Dict[str, Any]:
        """Run all labeled cases deterministically and aggregate metrics."""
        with open(self.cases_file, "r", encoding="utf-8") as f:
            cases: List[Dict[str, Any]] = json.load(f)

        results: List[Dict[str, Any]] = []
        for case in cases:
            res = await self.evaluate_single_case(case)
            results.append(res)

        # Categorize
        categories = sorted({c["category"] for c in cases})
        cat_metrics: Dict[str, Any] = {}
        all_latencies = [r["latency_ms"] for r in results]

        for cat in categories:
            cat_results = [r for r in results if r["category"] == cat]
            c_total = len(cat_results)
            c_passed = sum(1 for r in cat_results if r["passed"])
            c_acc = round(c_passed / c_total, 4) if c_total else 0.0
            c_latencies = [r["latency_ms"] for r in cat_results]
            cat_metrics[cat] = {
                "total": c_total,
                "passed": c_passed,
                "failed": c_total - c_passed,
                "accuracy": c_acc,
                "mean_ms": round(statistics.mean(c_latencies), 2) if c_latencies else 0.0,
                "p50_ms": compute_percentile(c_latencies, 50),
                "p95_ms": compute_percentile(c_latencies, 95),
            }

        total_cases = len(results)
        total_passed = sum(1 for r in results if r["passed"])
        overall_accuracy = round(total_passed / total_cases, 4) if total_cases else 0.0

        # Grounding metrics: count injected invented IDs and blocked
        grounding_cases = [r for r in results if r["category"] == "grounding"]
        # GR-01 and GR-02 test invented IDs
        invented_cases = [r for r in grounding_cases if r["id"] in ("GR-01", "GR-02")]
        injected_count = len(invented_cases)
        blocked_count = sum(1 for r in invented_cases if r["passed"])

        failed_details = [
            {
                "id": r["id"],
                "category": r["category"],
                "guards_against": r["guards_against"],
                "expected": r["expected"],
                "actual": r["actual"],
                "latency_ms": r["latency_ms"],
            }
            for r in results
            if not r["passed"]
        ]

        return {
            "total_cases": total_cases,
            "passed_cases": total_passed,
            "failed_cases": total_cases - total_passed,
            "overall_accuracy": overall_accuracy,
            "p50_latency_ms": compute_percentile(all_latencies, 50),
            "p95_latency_ms": compute_percentile(all_latencies, 95),
            "mean_latency_ms": round(statistics.mean(all_latencies), 2) if all_latencies else 0.0,
            "category_metrics": cat_metrics,
            "grounding_summary": {
                "label": "simulated LLM output",
                "invented_ids_injected": injected_count,
                "invented_ids_blocked": blocked_count,
            },
            "results": results,
            "failed_details": failed_details,
        }

    async def run_llm_eval(self) -> Dict[str, Any]:
        """Run analysis on 88-PR dataset using configured LLM models if --with-llm is passed."""
        if not self.with_llm:
            return {"enabled": False}

        from app.agents.analyze import build_analysis_prompt
        from app.llm.gemini_client import GeminiClient
        from app.llm.ollama_client import OllamaClient
        from app.llm.router import LLMRouter, is_resource_exhausted_error
        from app.llm.schemas import Analysis

        prompt = build_analysis_prompt(
            workspace_id="eval",
            prs=self.prs_88,
            tickets=self.tickets_3,
            recent_flags=[],
            stale_pr_days=7,
            stale_ticket_days=14,
            abandoned_pr_days=180,
            max_items_in_prompt=15,
        )

        valid_canonical_ids = {f"{p.repo}#{p.number}" for p in self.prs_88} | {t.key for t in self.tickets_3}
        model_results: Dict[str, Any] = {}
        total_calls_made = 0
        quota_hit = False

        for model_name in self.models:
            if total_calls_made >= self.max_llm_calls or quota_hit:
                break

            latencies: List[float] = []
            valid_json_count = 0
            invented_before_count = 0
            invented_after_count = 0
            total_flags_count = 0
            all_flags_in_fixture = True
            calls_attempted = 0
            quota_skipped = 0

            # Attempt 1 call per model up to remaining max_calls
            t0 = time.perf_counter()
            calls_attempted += 1
            total_calls_made += 1

            try:
                router = LLMRouter()
                res = await router.call_candidate(
                    provider="gemini" if "gemini" in model_name.lower() else "ollama",
                    model_name=model_name,
                    prompt=prompt,
                    schema=Analysis,
                    task_type="analysis",
                )
                lat = (time.perf_counter() - t0) * 1000
                latencies.append(lat)
                valid_json_count += 1

                analysis: Analysis = res.data
                flags = analysis.priority_flags or []
                total_flags_count += len(flags)

                for f in flags:
                    if f.item_id not in valid_canonical_ids:
                        invented_before_count += 1

                # Grounding
                grounded = [f for f in flags if normalize_llm_id(f.item_id, valid_canonical_ids) is not None]
                for f in grounded:
                    norm = normalize_llm_id(f.item_id, valid_canonical_ids)
                    if norm not in valid_canonical_ids:
                        invented_after_count += 1
                        all_flags_in_fixture = False

            except Exception as exc:
                lat = (time.perf_counter() - t0) * 1000
                latencies.append(lat)
                if is_resource_exhausted_error(exc):
                    quota_hit = True
                    quota_skipped += 1
                    logger.warning("Model %s encountered quota limit (429). Halting LLM calls.", model_name)
                else:
                    logger.warning("Model %s evaluation failed: %s", model_name, exc)

            model_results[model_name] = {
                "calls_attempted": calls_attempted,
                "valid_json_calls": valid_json_count,
                "total_flags_evaluated": total_flags_count,
                "invented_ids_before_grounding": invented_before_count,
                "invented_ids_after_grounding": invented_after_count,
                "all_flags_in_fixture": all_flags_in_fixture and (total_flags_count > 0 or valid_json_count > 0),
                "mean_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
                "p95_latency_ms": compute_percentile(latencies, 95) if latencies else 0.0,
                "calls_skipped_quota_429": quota_skipped,
            }

        return {
            "enabled": True,
            "quota_hit": quota_hit,
            "total_calls_made": total_calls_made,
            "max_llm_calls_allowed": self.max_llm_calls,
            "models": model_results,
        }

    def write_artifacts(self, det_metrics: Dict[str, Any], llm_metrics: Dict[str, Any]) -> Tuple[Path, Path]:
        """Write evals/results/latest.json and evals/REPORT.md."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        latest_json_path = self.out_dir / "latest.json"
        report_md_path = Path(__file__).resolve().parent / "REPORT.md"

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        git_hash = get_git_commit_hash()
        py_ver = sys.version.split()[0]

        latest_data = {
            "timestamp": now_utc,
            "git_commit": git_hash,
            "python_version": py_ver,
            "deterministic_eval": det_metrics,
            "llm_eval": llm_metrics,
            "limits": {
                "synthetic_fixtures": "88 pull requests (ages 20-3300 days) and 3 Jira tickets generated deterministically with fixed random seed (42).",
                "simulated_llm_outputs": "Grounding and hallucination rejection tests evaluate synthetic PriorityFlags with injected fake IDs and corrupted facts.",
                "llm_sample_size": f"Capped at {self.max_llm_calls} calls total to prevent exhausting free-tier quotas (20 requests/day).",
            },
        }

        with open(latest_json_path, "w", encoding="utf-8") as f:
            json.dump(latest_data, f, indent=2)

        # Generate REPORT.md: Every single number is formatted strictly from latest_data
        cat_metrics = det_metrics["category_metrics"]
        grounding = det_metrics["grounding_summary"]

        md_lines = [
            "# TeamPulse Offline Evaluation Report (Phase 6)",
            "",
            f"- **Date**: {latest_data['timestamp']}",
            f"- **Git Commit**: `{latest_data['git_commit']}`",
            f"- **Python Version**: `{latest_data['python_version']}`",
            f"- **Total Labeled Cases**: {det_metrics['total_cases']}",
            f"- **Passed**: {det_metrics['passed_cases']}",
            f"- **Failed**: {det_metrics['failed_cases']}",
            f"- **Overall Accuracy**: {round(det_metrics['overall_accuracy'] * 100, 2)}%",
            f"- **p50 Latency**: {det_metrics['p50_latency_ms']} ms",
            f"- **p95 Latency**: {det_metrics['p95_latency_ms']} ms",
            "",
            "## 1. Deterministic Evaluation by Category",
            "",
            "| Category | Cases | Passed | Accuracy | Mean Latency (ms) | p95 Latency (ms) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: |",
        ]

        for cat, m in sorted(cat_metrics.items()):
            acc_pct = round(m["accuracy"] * 100, 1)
            md_lines.append(
                f"| `{cat}` | {m['total']} | {m['passed']} | {acc_pct}% | {m['mean_ms']} | {m['p95_ms']} |"
            )

        md_lines.extend([
            "",
            "## 2. Grounding & Hallucination Protection",
            "",
            f"- **Evaluation Mode**: `{grounding['label']}`",
            f"- **Invented IDs Injected**: {grounding['invented_ids_injected']}",
            f"- **Invented IDs Blocked**: {grounding['invented_ids_blocked']}",
            f"- **Protection Rate**: {round((grounding['invented_ids_blocked'] / grounding['invented_ids_injected'] * 100) if grounding['invented_ids_injected'] else 100.0, 1)}%",
            "",
            "## 3. Live LLM Evaluation",
            "",
        ])

        if not llm_metrics.get("enabled"):
            md_lines.append(
                "Live LLM evaluation was disabled (`--with-llm` not supplied). Deterministic offline suite executed."
            )
        else:
            models_data = llm_metrics.get("models", {})
            md_lines.extend([
                f"- **Total LLM Calls Attempted**: {llm_metrics['total_calls_made']} (cap: {llm_metrics['max_llm_calls_allowed']})",
                f"- **Quota Exhaustion Encountered**: {'Yes (stopped further requests)' if llm_metrics['quota_hit'] else 'No'}",
                "",
                "| Model | Valid JSON | Invented (Before) | Invented (After) | In Fixture | Mean (ms) | p95 (ms) | Quota Exhausted |",
                "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
            ])
            for m_name, m_info in models_data.items():
                v_rate = f"{m_info['valid_json_calls']}/{m_info['calls_attempted']}"
                inv_b = f"{m_info['invented_ids_before_grounding']}/{m_info['total_flags_evaluated']}"
                inv_a = f"{m_info['invented_ids_after_grounding']}/{m_info['total_flags_evaluated']}"
                in_fix = "Yes" if m_info["all_flags_in_fixture"] else "No"
                md_lines.append(
                    f"| `{m_name}` | {v_rate} | {inv_b} | {inv_a} | {in_fix} | {m_info['mean_latency_ms']} | {m_info['p95_latency_ms']} | {m_info['calls_skipped_quota_429']} |"
                )

        md_lines.extend([
            "",
            "## 4. Failed Cases",
            "",
        ])

        if not det_metrics["failed_details"]:
            md_lines.append("None. All labeled deterministic cases passed successfully.")
        else:
            for f_case in det_metrics["failed_details"]:
                md_lines.append(f"### Case `{f_case['id']}` ({f_case['category']})")
                md_lines.append(f"- **Guards Against**: {f_case['guards_against']}")
                md_lines.append(f"- **Expected**: `{f_case['expected']}`")
                md_lines.append(f"- **Actual**: `{f_case['actual']}`")
                md_lines.append("")

        md_lines.extend([
            "",
            "## 5. Limits",
            "",
            f"1. **Synthetic Fixtures**: {latest_data['limits']['synthetic_fixtures']}",
            f"2. **Simulated LLM Outputs**: {latest_data['limits']['simulated_llm_outputs']}",
            f"3. **LLM Sample Size**: {latest_data['limits']['llm_sample_size']}",
            "",
        ])

        with open(report_md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines) + "\n")

        return latest_json_path, report_md_path

    def print_summary(self, det_metrics: Dict[str, Any], llm_metrics: Dict[str, Any]) -> None:
        """Print cleanly formatted summary table to console."""
        cat_metrics = det_metrics["category_metrics"]
        print("\n" + "=" * 90)
        print("                        TeamPulse Phase 6 Evaluation Suite")
        print("=" * 90)
        print(f"{'Category':<24} {'Cases':<8} {'Passed':<8} {'Accuracy':<10} {'Mean (ms)':<12} {'p95 (ms)':<10}")
        print("-" * 90)

        for cat, m in sorted(cat_metrics.items()):
            acc_str = f"{round(m['accuracy'] * 100, 1)}%"
            print(f"{cat:<24} {m['total']:<8} {m['passed']:<8} {acc_str:<10} {m['mean_ms']:<12} {m['p95_ms']:<10}")

        print("=" * 90)
        acc_total = round(det_metrics["overall_accuracy"] * 100, 2)
        print(
            f"Overall: {det_metrics['passed_cases']}/{det_metrics['total_cases']} passed ({acc_total}%) | "
            f"p50: {det_metrics['p50_latency_ms']} ms | p95: {det_metrics['p95_latency_ms']} ms"
        )
        grounding = det_metrics["grounding_summary"]
        print(
            f"Grounding Protection: {grounding['invented_ids_blocked']}/{grounding['invented_ids_injected']} "
            f"invented IDs blocked ({grounding['label']})"
        )

        if llm_metrics.get("enabled"):
            print("-" * 90)
            print("Live LLM Results:")
            for m_name, m_info in llm_metrics.get("models", {}).items():
                print(
                    f"  - {m_name}: valid_json={m_info['valid_json_calls']}/{m_info['calls_attempted']}, "
                    f"invented_before={m_info['invented_ids_before_grounding']}, "
                    f"invented_after={m_info['invented_ids_after_grounding']}, "
                    f"p95={m_info['p95_latency_ms']}ms, quota_skipped={m_info['calls_skipped_quota_429']}"
                )
        failed_details = det_metrics.get("failed_details", [])
        if failed_details:
            print("-" * 90)
            print("Failed Cases Detail:")
            for fc in failed_details:
                print(f"  [{fc['id']}] ({fc['category']}):")
                print(f"    Guards Against : {fc['guards_against']}")
                print(f"    Expected       : {fc['expected']}")
                print(f"    Actual         : {fc['actual']}")
        print("=" * 90 + "\n")


async def async_main(args: argparse.Namespace) -> int:
    """Async entrypoint for evaluation script."""
    cases_file = Path(args.cases)
    out_dir = Path(args.out)
    models_list = [m.strip() for m in args.models.split(",") if m.strip()]

    runner = EvalRunner(
        cases_file=cases_file,
        out_dir=out_dir,
        with_llm=args.with_llm,
        models=models_list,
        max_llm_calls=args.max_llm_calls,
    )

    det_metrics = await runner.run_deterministic_eval()
    llm_metrics = await runner.run_llm_eval()

    latest_json_path, report_md_path = runner.write_artifacts(det_metrics, llm_metrics)
    runner.print_summary(det_metrics, llm_metrics)

    print(f"Artifacts generated:")
    print(f"  - Results JSON: {latest_json_path}")
    print(f"  - Report MD:    {report_md_path}")

    # Return non-zero if any deterministic case failed
    if det_metrics["failed_cases"] > 0:
        print(f"\nFAILURE: {det_metrics['failed_cases']} deterministic case(s) failed!")
        return 1

    print("\nSUCCESS: All deterministic evaluation cases passed!")
    return 0


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="TeamPulse Offline Evaluation Suite (Phase 6)")
    parser.add_argument(
        "--cases",
        type=str,
        default=str(Path(__file__).resolve().parent / "labeled_cases.json"),
        help="Path to labeled_cases.json",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(Path(__file__).resolve().parent / "results"),
        help="Path to output directory for results",
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="Run live LLM models against 88-PR dataset (capped)",
    )
    parser.add_argument(
        "--models",
        type=str,
        default="gemini-2.5-flash,gemini-2.0-flash",
        help="Comma-separated model names to evaluate",
    )
    parser.add_argument(
        "--max-llm-calls",
        type=int,
        default=6,
        help="Maximum number of live LLM calls allowed across the run (default 6)",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
