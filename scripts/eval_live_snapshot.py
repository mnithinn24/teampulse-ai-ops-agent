"""Live evaluation snapshot script (Phase 6).

Sends 5 fixed questions to the live /query endpoint and prints the answer,
model_used, and latency to compare real runtime behavior against fixtures.
Read-only script; requires running server and .env configuration.
"""

import argparse
import sys
import time
from typing import List
import httpx

from app.config import get_settings

FIXED_QUESTIONS: List[str] = [
    "Which pull requests have been waiting the longest?",
    "How many stale pull requests are there?",
    "Are there any open pull requests?",
    "What is KAN-1?",
    "What is the weather today?",
]


def run_live_snapshot(endpoint_url: str) -> None:
    """Send fixed questions to live /query endpoint and display results."""
    settings = get_settings()
    api_key = settings.admin_api_key or "test-admin-key"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    print("=" * 80)
    print(" TeamPulse Live /query Evaluation Snapshot")
    print(f" Target Endpoint: {endpoint_url}")
    print("=" * 80)

    with httpx.Client(timeout=30.0) as client:
        for idx, question in enumerate(FIXED_QUESTIONS, 1):
            print(f"\n[{idx}/5] Query: \"{question}\"")
            t_start = time.perf_counter()
            try:
                response = client.post(
                    endpoint_url,
                    json={"query": question},
                    headers=headers,
                )
                latency_ms = round((time.perf_counter() - t_start) * 1000, 2)

                if response.status_code == 200:
                    data = response.json()
                    answer = data.get("answer", "")
                    model_used = data.get("model_used", "unknown")
                    server_lat = data.get("latency_ms", latency_ms)
                    print(f"  Status    : HTTP 200 OK")
                    print(f"  Model Used: {model_used}")
                    print(f"  Latency   : {server_lat} ms (roundtrip: {latency_ms} ms)")
                    print(f"  Answer    : {answer}")
                else:
                    print(f"  Status    : HTTP {response.status_code}")
                    print(f"  Response  : {response.text[:200]}")
            except httpx.ConnectError:
                print(f"  Error     : Connection refused to {endpoint_url}. Is the FastAPI server running?")
                print("  Hint      : Start the server using 'uvicorn app.api.main:app --port 8000'")
                break
            except Exception as exc:
                print(f"  Error     : {exc}")

    print("\n" + "=" * 80)
    print(" Live evaluation snapshot completed.")
    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Query live TeamPulse /query endpoint with 5 fixed questions.")
    parser.add_argument(
        "--url",
        type=str,
        default="http://localhost:8000/query",
        help="Full URL of the /query endpoint (default: http://localhost:8000/query)",
    )
    args = parser.parse_args()
    run_live_snapshot(args.url)


if __name__ == "__main__":
    main()
