"""CLI script to display run metrics, averages, and p50/p95 latency percentiles."""

import argparse
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
from sqlalchemy import select

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.db.database import get_db_session
from app.db.models import AgentRun


def compute_percentile(data: List[float], p: float) -> float:
    """Calculate the p-th percentile of a list of floats using linear interpolation.

    Args:
        data: List of numeric values.
        p: Percentile between 0 and 100.

    Returns:
        The calculated percentile value, or 0.0 if data is empty.
    """
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    k = (n - 1) * (p / 100.0)
    floor_idx = int(k)
    ceil_idx = floor_idx + 1
    if ceil_idx < n:
        weight = k - floor_idx
        return sorted_data[floor_idx] + weight * (sorted_data[ceil_idx] - sorted_data[floor_idx])
    return sorted_data[-1]


def calculate_metrics_summary(runs: List[AgentRun]) -> Dict[str, Any]:
    """Compute averages and p50 / p95 percentiles across a list of AgentRun records.

    Args:
        runs: List of AgentRun model instances.

    Returns:
        Dictionary containing aggregated latency and operational metrics.
    """
    if not runs:
        return {
            "total_runs": 0,
            "total_latency": {"avg": 0.0, "p50": 0.0, "p95": 0.0},
            "collect_latency": {"avg": 0.0, "p50": 0.0, "p95": 0.0},
            "analyze_latency": {"avg": 0.0, "p50": 0.0, "p95": 0.0},
            "act_latency": {"avg": 0.0, "p50": 0.0, "p95": 0.0},
            "fallback_runs": 0,
            "total_items_collected": 0,
            "total_flags_created": 0,
        }

    total_lats = [r.total_latency_ms for r in runs if r.total_latency_ms is not None]
    collect_lats = [r.collect_latency_ms for r in runs if r.collect_latency_ms is not None]
    analyze_lats = [r.analyze_latency_ms for r in runs if r.analyze_latency_ms is not None]
    act_lats = [r.act_latency_ms for r in runs if r.act_latency_ms is not None]

    fallback_count = sum(1 for r in runs if bool(r.fallback_used))
    total_items = sum(r.items_collected or 0 for r in runs)
    total_flags = sum(r.flags_created or 0 for r in runs)

    def stats(series: List[float]) -> Dict[str, float]:
        if not series:
            return {"avg": 0.0, "p50": 0.0, "p95": 0.0}
        return {
            "avg": round(sum(series) / len(series), 2),
            "p50": round(compute_percentile(series, 50.0), 2),
            "p95": round(compute_percentile(series, 95.0), 2),
        }

    return {
        "total_runs": len(runs),
        "total_latency": stats(total_lats),
        "collect_latency": stats(collect_lats),
        "analyze_latency": stats(analyze_lats),
        "act_latency": stats(act_lats),
        "fallback_runs": fallback_count,
        "total_items_collected": total_items,
        "total_flags_created": total_flags,
    }


def print_metrics_table(summary: Dict[str, Any], n_requested: int) -> None:
    """Print formatted ASCII table of run metrics."""
    print("=" * 65)
    print(f" TeamPulse Pipeline Run Metrics (Last {summary['total_runs']} runs, requested: {n_requested})")
    print("=" * 65)

    if summary["total_runs"] == 0:
        print(" No completed runs found in database.")
        print("=" * 65)
        return

    print(f" Total Work Items Collected : {summary['total_items_collected']}")
    print(f" Total Priority Flags Raised: {summary['total_flags_created']}")
    print(f" Runs Using LLM Fallback    : {summary['fallback_runs']} / {summary['total_runs']}")
    print("-" * 65)
    print(f" {'Metric / Node':<20} | {'Average (ms)':<12} | {'p50 (ms)':<10} | {'p95 (ms)':<10}")
    print("-" * 65)

    for name, key in [
        ("Collect Node", "collect_latency"),
        ("Analyze Node", "analyze_latency"),
        ("Act Node", "act_latency"),
        ("Total Pipeline", "total_latency"),
    ]:
        s = summary[key]
        print(f" {name:<20} | {s['avg']:<12.2f} | {s['p50']:<10.2f} | {s['p95']:<10.2f}")

    print("=" * 65)


def main() -> int:
    """CLI entry point for scripts/show_metrics.py."""
    parser = argparse.ArgumentParser(description="Display TeamPulse pipeline run metrics and latency percentiles.")
    parser.add_argument(
        "-n",
        "--runs",
        type=int,
        default=10,
        help="Number of recent runs to evaluate (default: 10)",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        type=str,
        default=None,
        help="Optional workspace filter",
    )
    args = parser.parse_args()

    with get_db_session() as session:
        query = select(AgentRun).where(AgentRun.status == "completed").order_by(AgentRun.id.desc())
        if args.workspace:
            query = query.where(AgentRun.workspace_id == args.workspace)
        query = query.limit(args.runs)
        runs = list(session.scalars(query).all())

    summary = calculate_metrics_summary(runs)
    print_metrics_table(summary, args.runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
