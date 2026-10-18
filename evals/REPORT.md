# TeamPulse Offline Evaluation Report (Phase 6)

- **Date**: 2026-10-16T23:29:40Z
- **Git Commit**: `5aaed274e4167a539a5ea874494cf0dccd710bb5`
- **Python Version**: `3.14.6`
- **Total Labeled Cases**: 48
- **Passed**: 48
- **Failed**: 0
- **Overall Accuracy**: 100.0%
- **p50 Latency**: 0.81 ms
- **p95 Latency**: 38.67 ms

## 1. Deterministic Evaluation by Category

| Category | Cases | Passed | Accuracy | Mean Latency (ms) | p95 Latency (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `action_safety` | 5 | 5 | 100.0% | 7.39 | 28.32 |
| `briefing_wording` | 5 | 5 | 100.0% | 0.11 | 0.22 |
| `failure_handling` | 5 | 5 | 100.0% | 0.58 | 1.34 |
| `grounding` | 6 | 6 | 100.0% | 0.08 | 0.12 |
| `query_count` | 6 | 6 | 100.0% | 36.63 | 39.77 |
| `query_lookup` | 5 | 5 | 100.0% | 36.62 | 38.64 |
| `query_out_of_scope` | 5 | 5 | 100.0% | 35.71 | 38.04 |
| `query_rank` | 5 | 5 | 100.0% | 45.42 | 78.42 |
| `stale_detection` | 6 | 6 | 100.0% | 0.0 | 0.0 |

## 2. Grounding & Hallucination Protection

- **Evaluation Mode**: `simulated LLM output`
- **Invented IDs Injected**: 2
- **Invented IDs Blocked**: 2
- **Protection Rate**: 100.0%

## 3. Live LLM Evaluation

- **Total LLM Calls Attempted**: 2 (cap: 6)
- **Quota Exhaustion Encountered**: No

| Model | Valid JSON | Invented (Before) | Invented (After) | In Fixture | Mean (ms) | p95 (ms) | Quota Exhausted |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `gemini-2.5-flash` | 0/1 | 0/0 | 0/0 | No | 312.88 | 312.88 | 0 |
| `gemini-2.0-flash` | 0/1 | 0/0 | 0/0 | No | 396.52 | 396.52 | 0 |

## 4. Failed Cases

None. All labeled deterministic cases passed successfully.

## 5. Limits

1. **Synthetic Fixtures**: 88 pull requests (ages 20-3300 days) and 3 Jira tickets generated deterministically with fixed random seed (42).
2. **Simulated LLM Outputs**: Grounding and hallucination rejection tests evaluate synthetic PriorityFlags with injected fake IDs and corrupted facts.
3. **LLM Sample Size**: Capped at 6 calls total to prevent exhausting free-tier quotas (20 requests/day).

