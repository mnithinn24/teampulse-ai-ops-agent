# TeamPulse Offline Evaluation Report (Phase 6)

- **Date**: 2026-10-16T11:56:56Z
- **Git Commit**: `f4dadb8ae6b0b343d18909a9556f0a8f006c3fef`
- **Python Version**: `3.14.6`
- **Total Labeled Cases**: 48
- **Passed**: 48
- **Failed**: 0
- **Overall Accuracy**: 100.0%
- **p50 Latency**: 0.55 ms
- **p95 Latency**: 40.13 ms

## 1. Deterministic Evaluation by Category

| Category | Cases | Passed | Accuracy | Mean Latency (ms) | p95 Latency (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `action_safety` | 5 | 5 | 100.0% | 3.7 | 13.83 |
| `briefing_wording` | 5 | 5 | 100.0% | 0.09 | 0.21 |
| `failure_handling` | 5 | 5 | 100.0% | 0.49 | 1.26 |
| `grounding` | 6 | 6 | 100.0% | 0.06 | 0.1 |
| `query_count` | 6 | 6 | 100.0% | 29.02 | 30.54 |
| `query_lookup` | 5 | 5 | 100.0% | 31.54 | 42.08 |
| `query_out_of_scope` | 5 | 5 | 100.0% | 32.42 | 40.37 |
| `query_rank` | 5 | 5 | 100.0% | 45.15 | 83.25 |
| `stale_detection` | 6 | 6 | 100.0% | 0.0 | 0.01 |

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
| `gemini-2.5-flash` | 0/1 | 0/0 | 0/0 | No | 262.71 | 262.71 | 0 |
| `gemini-2.0-flash` | 0/1 | 0/0 | 0/0 | No | 136.73 | 136.73 | 0 |

## 4. Failed Cases

None. All labeled deterministic cases passed successfully.

## 5. Limits

1. **Synthetic Fixtures**: 88 pull requests (ages 20-3300 days) and 3 Jira tickets generated deterministically with fixed random seed (42).
2. **Simulated LLM Outputs**: Grounding and hallucination rejection tests evaluate synthetic PriorityFlags with injected fake IDs and corrupted facts.
3. **LLM Sample Size**: Capped at 6 calls total to prevent exhausting free-tier quotas (20 requests/day).

