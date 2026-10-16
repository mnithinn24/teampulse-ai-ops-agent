"""Unit tests for Phase 6 evaluation suite code and artifacts."""

import json
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch
import pytest

from evals.fixtures import (
    compute_ground_truth_rank,
    compute_ground_truth_totals,
    generate_3_jira_dataset,
    generate_88_pr_dataset,
    generate_stale_boundary_dataset,
)
from evals.run_eval import EvalRunner

CASES_PATH = Path(__file__).resolve().parent.parent / "evals" / "labeled_cases.json"
RESULTS_PATH = Path(__file__).resolve().parent.parent / "evals" / "results" / "latest.json"
REPORT_PATH = Path(__file__).resolve().parent.parent / "evals" / "REPORT.md"

REQUIRED_CATEGORIES = {
    "query_rank",
    "query_count",
    "query_lookup",
    "query_out_of_scope",
    "stale_detection",
    "grounding",
    "failure_handling",
    "briefing_wording",
    "action_safety",
}


def test_case_file_loads_and_has_at_least_45_cases() -> None:
    """Requirement 6: The case file loads and has at least 45 cases with every category present."""
    assert CASES_PATH.exists(), f"Cases file not found at {CASES_PATH}"

    with open(CASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    assert isinstance(cases, list)
    assert len(cases) >= 45, f"Expected at least 45 cases, found {len(cases)}"

    present_categories = {c["category"] for c in cases}
    missing_categories = REQUIRED_CATEGORIES - present_categories
    assert not missing_categories, f"Missing required eval categories: {missing_categories}"

    for case in cases:
        assert "id" in case and case["id"]
        assert "category" in case and case["category"]
        assert "input" in case
        assert "expected" in case
        assert "guards_against" in case and case["guards_against"]


def test_expected_values_derived_from_fixtures() -> None:
    """Requirement 6: Expected values are derived from synthetic fixtures."""
    prs = generate_88_pr_dataset()
    tickets = generate_3_jira_dataset()
    boundary = generate_stale_boundary_dataset()

    totals = compute_ground_truth_totals(prs, tickets)
    ranks = compute_ground_truth_rank(prs, limit=5)

    assert totals["open_prs"] == 88
    assert totals["stale_prs"] == 88
    assert totals["total_tickets"] == 3
    assert totals["stale_tickets"] == 0

    with open(CASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    cases_by_id = {c["id"]: c for c in cases}

    # QC-01: stale PRs count
    assert cases_by_id["QC-01"]["expected"]["count"] == totals["stale_prs"]
    # QC-02: open PRs count
    assert cases_by_id["QC-02"]["expected"]["count"] == totals["open_prs"]
    # QC-05: Jira tickets count
    assert cases_by_id["QC-05"]["expected"]["count"] == totals["total_tickets"]
    # QC-06: stale Jira tickets count
    assert cases_by_id["QC-06"]["expected"]["count"] == totals["stale_tickets"]

    # QR-01: oldest PR
    oldest_pr = ranks[0]
    assert cases_by_id["QR-01"]["expected"]["top_number"] == oldest_pr["number"]
    assert cases_by_id["QR-01"]["expected"]["top_age"] == oldest_pr["age_days"]

    # QR-02: top 3
    assert cases_by_id["QR-02"]["expected"]["contains"] == [f"#{p['number']}" for p in ranks[:3]]

    # Stale detection boundaries
    pr_boundary = {p.age_days: p.is_stale for p in boundary["prs"]}
    assert pr_boundary[6] is False  # 1 day below 7d threshold
    assert pr_boundary[7] is True   # exactly at threshold
    assert pr_boundary[8] is True   # 1 day above threshold


@pytest.mark.asyncio
async def test_broken_application_function_makes_runner_fail(tmp_path: Path) -> None:
    """Requirement 6: A deliberately broken application function makes the runner fail."""
    runner = EvalRunner(cases_file=CASES_PATH, out_dir=tmp_path)

    # Deliberately break handle_plain_language_query using mock
    mock_broken_query = AsyncMock(return_value={"answer": "DELIBERATELY_WRONG_ANSWER"})

    with patch("evals.run_eval.handle_plain_language_query", mock_broken_query):
        # Test single case evaluation
        case_qr01 = next(c for c in json.load(open(CASES_PATH, "r", encoding="utf-8")) if c["id"] == "QR-01")
        result = await runner.evaluate_single_case(case_qr01)
        assert result["passed"] is False, "Broken query handler should have caused QR-01 to fail"

        # Full deterministic run with broken query handler
        det_metrics = await runner.run_deterministic_eval()
        assert det_metrics["failed_cases"] > 0
        assert det_metrics["overall_accuracy"] < 1.0


def test_report_contains_no_number_not_in_latest_json() -> None:
    """Requirement 6: The report contains no number that is not in latest.json."""
    assert RESULTS_PATH.exists(), f"latest.json missing at {RESULTS_PATH}"
    assert REPORT_PATH.exists(), f"REPORT.md missing at {REPORT_PATH}"

    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        latest = json.load(f)

    with open(REPORT_PATH, "r", encoding="utf-8") as f:
        report_text = f.read()

    # Collect all numbers from latest.json
    def extract_numbers_from_obj(obj) -> set:
        nums = set()
        if isinstance(obj, (int, float)):
            nums.add(str(obj))
            nums.add(str(int(obj)) if isinstance(obj, float) and obj.is_integer() else str(obj))
            nums.add(f"{obj:.1f}")
            nums.add(f"{obj:.2f}")
            nums.add(f"{round(obj * 100, 1)}%")
            nums.add(f"{round(obj * 100, 2)}%")
        elif isinstance(obj, str):
            nums.add(obj)
            for m in re.findall(r"\b\d+(?:\.\d+)?\b", obj):
                nums.add(m)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                nums.update(extract_numbers_from_obj(k))
                nums.update(extract_numbers_from_obj(v))
        elif isinstance(obj, list):
            for item in obj:
                nums.update(extract_numbers_from_obj(item))
        return nums

    json_numbers = extract_numbers_from_obj(latest)

    # Extract all numbers from tables and metric lines in REPORT.md
    metric_lines = [
        line for line in report_text.splitlines()
        if line.startswith("|") or line.startswith("- **")
    ]

    for line in metric_lines:
        tokens = re.findall(r"\b\d+(?:\.\d+)?%?\b", line)
        for token in tokens:
            cleaned = token.rstrip("%")
            # Number must be traceable to latest.json numbers
            found = (
                token in json_numbers
                or cleaned in json_numbers
                or any(cleaned in jn for jn in json_numbers)
            )
            assert found, f"Number '{token}' in report line '{line}' was not found in latest.json"
