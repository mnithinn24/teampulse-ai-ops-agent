"""Canonical item ID generation and LLM ID normalization."""

import re
from typing import Optional, Set, Union

JIRA_KEY_REGEX = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b", re.IGNORECASE)
FULL_PR_REGEX = re.compile(
    r"\b([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)(?:\s+(?:PR|pull\s+request))?\s*#?(\d+)\b",
    re.IGNORECASE,
)
PARTIAL_PR_REGEX = re.compile(
    r"(?:(?:pull\s+request|PR)\s*#?|#)(\d+)\b",
    re.IGNORECASE,
)
BARE_NUM_REGEX = re.compile(r"^\s*(\d+)\s*$")


def canonical_id(
    source: str,
    identifier: Union[int, str],
    repo: Optional[str] = None,
) -> str:
    """Generate canonical item ID for GitHub PRs and Jira issues.

    Canonical formats:
    - GitHub PRs: 'owner/repo#123' (e.g. psf/requests#6454)
    - Jira issues: 'KEY-123' (e.g. KAN-1)

    Args:
        source: 'github' or 'jira'.
        identifier: PR number/id or Jira issue key.
        repo: Repository name (required for GitHub source).

    Returns:
        Canonical ID string.
    """
    src = source.strip().lower()
    if src == "github":
        clean_repo = (repo or "").strip()
        clean_id = str(identifier).strip()
        if clean_repo:
            return f"{clean_repo}#{clean_id}"
        return clean_id
    elif src == "jira":
        return str(identifier).strip().upper()
    return str(identifier).strip()


def normalize_llm_id(text: str, valid_ids: Set[str]) -> Optional[str]:
    """Map common LLM output formats to the canonical item ID.

    Accepts formats such as:
    - 'psf/requests PR #6454', 'psf/requests#6454', 'psf/requests/pull/6454'
    - 'PR #6454', '#6454', 'pull request 6454' (if unambiguous across valid_ids)
    - Bare number '6454' (only if it matches exactly one valid ID)
    - Jira formats: 'KAN-1', '[KAN-1]', 'issue KAN-1', 'ticket KAN-1'

    Args:
        text: Candidate ID text from LLM response.
        valid_ids: Set of canonical valid IDs currently tracked.

    Returns:
        Canonical ID if matched unambiguously, or None.
    """
    if not text:
        return None

    raw = text.strip()

    # 1. Exact match against canonical IDs
    if raw in valid_ids:
        return raw
    if raw.upper() in valid_ids:
        return raw.upper()

    # 2. Jira issue format: [KAN-1], ticket KAN-1, issue KAN-1
    jira_match = JIRA_KEY_REGEX.search(raw)
    if jira_match:
        key = jira_match.group(1).upper()
        if key in valid_ids:
            return key

    # 3. Full GitHub PR format: owner/repo PR #6454 or owner/repo#6454
    full_match = FULL_PR_REGEX.search(raw)
    if full_match:
        repo = full_match.group(1)
        num = full_match.group(2)
        candidate = f"{repo}#{num}"
        if candidate in valid_ids:
            return candidate

    # 4. Partial PR format: PR #6454, #6454, pull request 6454
    partial_match = PARTIAL_PR_REGEX.search(raw)
    if partial_match:
        num = partial_match.group(1)
        matches = [cid for cid in valid_ids if cid.endswith(f"#{num}")]
        if len(matches) == 1:
            return matches[0]
        return None

    # 5. Bare number: e.g. '6454'
    bare_match = BARE_NUM_REGEX.match(raw)
    if bare_match:
        num = bare_match.group(1)
        matches = [cid for cid in valid_ids if cid.endswith(f"#{num}")]
        if len(matches) == 1:
            return matches[0]
        return None

    return None
