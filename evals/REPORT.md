# TeamPulse Offline Evaluation Report (Phase 6)

- **Date**: 2026-10-16T10:14:55Z
- **Git Commit**: `2b300f83abfff31e2c0a2e8cf0a9de064e9f02b8`
- **Python Version**: `3.14.6`
- **Total Labeled Cases**: 48
- **Passed**: 46
- **Failed**: 2
- **Overall Accuracy**: 95.83%
- **p50 Latency**: 0.93 ms
- **p95 Latency**: 38.68 ms

## 1. Deterministic Evaluation by Category

| Category | Cases | Passed | Accuracy | Mean Latency (ms) | p95 Latency (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `action_safety` | 5 | 5 | 100.0% | 5.49 | 20.33 |
| `briefing_wording` | 5 | 5 | 100.0% | 0.15 | 0.29 |
| `failure_handling` | 5 | 5 | 100.0% | 0.63 | 1.61 |
| `grounding` | 6 | 6 | 100.0% | 0.08 | 0.12 |
| `query_count` | 6 | 4 | 66.7% | 35.36 | 36.7 |
| `query_lookup` | 5 | 5 | 100.0% | 34.85 | 36.5 |
| `query_out_of_scope` | 5 | 5 | 100.0% | 35.22 | 37.64 |
| `query_rank` | 5 | 5 | 100.0% | 46.44 | 75.95 |
| `stale_detection` | 6 | 6 | 100.0% | 0.0 | 0.01 |

## 2. Grounding & Hallucination Protection

- **Evaluation Mode**: `simulated LLM output`
- **Invented IDs Injected**: 2
- **Invented IDs Blocked**: 2
- **Protection Rate**: 100.0%

## 3. Live LLM Evaluation

Live LLM evaluation was disabled (`--with-llm` not supplied). Deterministic offline suite executed.

## 4. Failed Cases

### Case `QC-03` (query_count)
- **Guards Against**: Author filtering failed or missed items outside the first page/prompt slice.
- **Expected**: `{'count': 15, 'contains': ['15']}`
- **Actual**: `There are 88 open pull requests.`

### Case `QC-04` (query_count)
- **Guards Against**: Author name matching was case-sensitive or truncated.
- **Expected**: `{'count': 15, 'contains': ['15']}`
- **Actual**: `There are 88 open pull requests.`


## 5. Limits

1. **Synthetic Fixtures**: 88 pull requests (ages 20-3300 days) and 3 Jira tickets generated deterministically with fixed random seed (42).
2. **Simulated LLM Outputs**: Grounding and hallucination rejection tests evaluate synthetic PriorityFlags with injected fake IDs and corrupted facts.
3. **LLM Sample Size**: Capped at 6 calls total to prevent exhausting free-tier quotas (20 requests/day).

