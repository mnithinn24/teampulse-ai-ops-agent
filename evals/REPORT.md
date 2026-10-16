# TeamPulse Offline Evaluation Report (Phase 6)

- **Date**: 2026-10-16T10:08:02Z
- **Git Commit**: `1d4cf0d55a9e4564e1b2d7a4cdb878a9c6091d68`
- **Python Version**: `3.14.6`
- **Total Labeled Cases**: 48
- **Passed**: 45
- **Failed**: 3
- **Overall Accuracy**: 93.75%
- **p50 Latency**: 0.9 ms
- **p95 Latency**: 66.32 ms

## 1. Deterministic Evaluation by Category

| Category | Cases | Passed | Accuracy | Mean Latency (ms) | p95 Latency (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `action_safety` | 5 | 5 | 100.0% | 4.34 | 16.0 |
| `briefing_wording` | 5 | 5 | 100.0% | 0.1 | 0.22 |
| `failure_handling` | 5 | 5 | 100.0% | 0.54 | 1.3 |
| `grounding` | 6 | 6 | 100.0% | 0.08 | 0.12 |
| `query_count` | 6 | 4 | 66.7% | 49.43 | 68.13 |
| `query_lookup` | 5 | 5 | 100.0% | 43.06 | 44.58 |
| `query_out_of_scope` | 5 | 5 | 100.0% | 35.76 | 38.66 |
| `query_rank` | 5 | 4 | 80.0% | 981.46 | 3769.17 |
| `stale_detection` | 6 | 6 | 100.0% | 0.0 | 0.01 |

## 2. Grounding & Hallucination Protection

- **Evaluation Mode**: `simulated LLM output`
- **Invented IDs Injected**: 2
- **Invented IDs Blocked**: 2
- **Protection Rate**: 100.0%

## 3. Live LLM Evaluation

Live LLM evaluation was disabled (`--with-llm` not supplied). Deterministic offline suite executed.

## 4. Failed Cases

### Case `QR-04` (query_rank)
- **Guards Against**: Ranking by last activity confused age_days with last_commit_days.
- **Expected**: `{'contains': ['#88'], 'metric': 'last_commit_days'}`
- **Actual**: `Found matching items: mnithinn24/teampulse-ai-ops-agent#1: Synthetic pull request #1 addressing ops task; mnithinn24/teampulse-ai-ops-agent#2: Synthetic pull request #2 addressing ops task; mnithinn24/teampulse-ai-ops-agent#3: Synthetic pull request #3 addressing ops task`

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

