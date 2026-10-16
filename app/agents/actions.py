"""Action executors for approved TeamPulse priority flags."""

import logging
import re
from typing import Optional

from app.config import get_settings
from app.core.constants import ITEM_TYPE_GITHUB_PR, ITEM_TYPE_JIRA_ISSUE
from app.core.errors import ActionError
from app.db.models import Flag
from app.integrations.jira_client import JiraClient

logger = logging.getLogger(__name__)


def build_pr_url(canonical_id: str) -> str:
    """Build GitHub PR URL from canonical ID (e.g. 'owner/repo#123').

    Args:
        canonical_id: GitHub PR canonical identifier.

    Returns:
        Full GitHub pull request URL.
    """
    match = re.match(r"^([^/]+/[^#]+)#(\d+)$", canonical_id.strip())
    if match:
        repo, num = match.groups()
        return f"https://github.com/{repo}/pull/{num}"
    return f"https://github.com/{canonical_id}"


async def execute_approved_action(
    flag: Flag,
    jira_client: Optional[JiraClient] = None,
    dry_run: Optional[bool] = None,
) -> str:
    """Execute the external action associated with an approved flag.

    Actions:
    - Jira flag: Add comment on that issue with the suggested action text.
    - GitHub PR flag: Create a Jira issue in JIRA_PROJECT_KEY with summary
      "Triage stale PR <canonical id>" and description containing reason, age,
      author, and GitHub PR link. (There is no GitHub write access).

    Respects DRY_RUN_ACTIONS setting.

    Args:
        flag: Database Flag model instance.
        jira_client: Optional pre-configured JiraClient.
        dry_run: Optional dry-run override.

    Returns:
        String description of the result (e.g., 'Created KAN-42' or 'Commented on KAN-12').

    Raises:
        ActionError: If execution fails.
    """
    settings = get_settings()
    is_dry_run = dry_run if dry_run is not None else settings.dry_run_actions

    item_id = flag.item_id
    item_type = flag.item_type or (ITEM_TYPE_GITHUB_PR if "#" in item_id else ITEM_TYPE_JIRA_ISSUE)

    logger.info(
        "Executing approved action for flag id=%s, item_id=%s, type=%s (dry_run=%s)",
        flag.id,
        item_id,
        item_type,
        is_dry_run,
    )

    try:
        if item_type in (ITEM_TYPE_JIRA_ISSUE, "jira_ticket"):
            comment_text = (
                flag.suggested_action
                or f"TeamPulse Ops Agent: {flag.flag_reason}. Please review."
            )
            if is_dry_run:
                logger.info(
                    "[DRY_RUN] Would add comment to Jira issue %s: %s",
                    item_id,
                    comment_text,
                )
                return f"[DRY RUN] would comment on {item_id}"

            client = jira_client or JiraClient()
            try:
                await client.add_comment(issue_key=item_id, comment_text=comment_text)
                return f"Commented on {item_id}"
            finally:
                if jira_client is None:
                    await client.close()

        elif item_type in (ITEM_TYPE_GITHUB_PR, "github_pr"):
            summary = f"Triage stale PR {item_id}"
            pr_url = build_pr_url(item_id)
            age = flag.evidence_age_days if flag.evidence_age_days is not None else "N/A"
            author = flag.evidence_author or "Unknown"
            reason = flag.flag_reason or "Stale activity"
            suggested_action = flag.suggested_action or "Review and merge or close"

            description = (
                f"Stale Pull Request: {pr_url}\n"
                f"Author: {author}\n"
                f"Age: {age} days\n"
                f"Reason: {reason}\n"
                f"Suggested Action: {suggested_action}"
            )

            project_key = settings.jira_project_key or "PROJ"

            if is_dry_run:
                logger.info(
                    "[DRY_RUN] Would create Jira issue in project %s with summary: %s",
                    project_key,
                    summary,
                )
                return "[DRY RUN] would create a Jira issue"

            client = jira_client or JiraClient()
            try:
                # Duplicate ticket check: search Jira in the project for an open issue
                # whose summary contains "Triage stale PR <canonical id>"
                jql = f"project = '{project_key}' AND resolution = Unresolved"
                try:
                    open_issues = await client.search_issues(jql=jql)
                except Exception as search_err:
                    logger.warning("Failed to search Jira for duplicates: %s", search_err)
                    open_issues = []

                target_phrase = f"Triage stale PR {item_id}"
                duplicate = None
                if isinstance(open_issues, (list, tuple)):
                    duplicate = next(
                        (
                            i
                            for i in open_issues
                            if target_phrase.lower() in (getattr(i, "title", "") or "").lower()
                        ),
                        None,
                    )

                if duplicate:
                    logger.info("Open issue already exists for PR %s: %s", item_id, duplicate.key)
                    return f"Already tracked as {duplicate.key}"

                created_issue = await client.create_issue(
                    project_key=project_key,
                    summary=summary,
                    description=description,
                )
                return f"Created {created_issue.key}"
            finally:
                if jira_client is None:
                    await client.close()

        else:
            raise ActionError(
                message=f"Unsupported item type for action execution: {item_type}",
                details={"item_id": item_id, "item_type": item_type},
            )

    except Exception as exc:
        logger.error("Failed to execute approved action for flag %s: %s", flag.id, exc)
        if isinstance(exc, ActionError):
            raise
        raise ActionError(
            message=f"Action execution failed for {item_id}: {exc}",
            details={"flag_id": flag.id, "item_id": item_id, "error": str(exc)},
        ) from exc
