"""PR Completion to Slack Alert and Interactive Approval/Decline Handler (Phase 9)."""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.db.database import get_session_factory
from app.db.models import ActionTaken, utc_now
from app.integrations.github_client import GitHubClient
from app.integrations.jira_client import JiraClient
from app.integrations.slack_client import SlackClient

logger = logging.getLogger(__name__)

JIRA_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]{1,30}-\d+)\b")


def extract_jira_keys(text: str) -> List[str]:
    """Extract distinct Jira ticket keys from PR branch, title, or body.

    Examples:
    - 'feature/KAN-42-rag-pipeline' -> ['KAN-42']
    - '[KAN-10] Fix medical document chunking' -> ['KAN-10']
    - 'Fixes PROJ-101 and PROJ-102' -> ['PROJ-101', 'PROJ-102']

    Args:
        text: Free-form input string.

    Returns:
        List of unique uppercase Jira issue keys.
    """
    if not text:
        return []
    matches = JIRA_KEY_PATTERN.findall(text)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for key in matches:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


async def fetch_jira_ticket_context(
    jira_key: str,
    jira_client: Optional[JiraClient] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch Jira issue details to enrich the PR Slack alert card.

    Args:
        jira_key: Jira issue key (e.g. 'KAN-42').
        jira_client: Optional pre-configured JiraClient.

    Returns:
        Dictionary with key, title, status, url, and assignee, or None if lookup fails.
    """
    settings = get_settings()
    base_url = (settings.jira_base_url or "").rstrip("/")
    fallback_url = f"{base_url}/browse/{jira_key}" if base_url else ""

    client = jira_client or JiraClient()
    try:
        issue = await client.get_issue(jira_key)
        return {
            "key": issue.key,
            "title": issue.title,
            "status": issue.status,
            "assignee": issue.assignee or "Unassigned",
            "url": issue.url or fallback_url,
        }
    except Exception as exc:
        logger.warning("Could not fetch Jira issue context for %s: %s", jira_key, exc)
        return {
            "key": jira_key,
            "title": "Jira Issue",
            "status": "Unknown",
            "assignee": "Unassigned",
            "url": fallback_url,
        }
    finally:
        if jira_client is None:
            await client.close()


def build_pr_slack_blocks(
    repo: str,
    pr_number: int,
    pr_title: str,
    pr_author: str,
    pr_url: str,
    head_ref: str = "feature",
    base_ref: str = "main",
    jira_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build rich Slack Block Kit layout for PR alert with Approve & Merge / Decline buttons.

    Args:
        repo: Repository identifier in 'owner/repo' format.
        pr_number: Pull request number.
        pr_title: Pull request title.
        pr_author: GitHub author login.
        pr_url: Web URL to pull request.
        head_ref: Head branch name.
        base_ref: Target base branch name.
        jira_info: Optional dict with linked Jira ticket context.

    Returns:
        List of Slack Block Kit block dictionaries.
    """
    composite_val = f"{repo}#{pr_number}"
    jira_str = ""
    if jira_info and jira_info.get("key"):
        composite_val += f"|{jira_info['key']}"
        jira_str = f"• :link: *Linked Jira Ticket:* <{jira_info['url']}|{jira_info['key']}: {jira_info.get('title', '')}> (Status: *{jira_info.get('status', 'Open')}*)"

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🚀 Pull Request Ready for Review",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Pull Request:* <{pr_url}|*{repo}#{pr_number}: {pr_title}*>\n"
                    f"• *Author:* `@{pr_author}`\n"
                    f"• *Branches:* `{head_ref}` → `{base_ref}`\n"
                    f"{jira_str}"
                ).strip(),
            },
        },
        {
            "type": "actions",
            "block_id": f"pr_actions_{repo}_{pr_number}",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Approve & Merge",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "pr_approve_and_merge",
                    "value": composite_val,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Decline",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": "pr_decline",
                    "value": composite_val,
                },
            ],
        },
    ]

    return blocks


async def notify_new_pull_request(
    repo: str,
    pr_number: int,
    pr_title: str,
    pr_author: str,
    pr_url: str,
    head_ref: str = "feature",
    base_ref: str = "main",
    pr_body: str = "",
    slack_client: Optional[SlackClient] = None,
    jira_client: Optional[JiraClient] = None,
    channel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a newly opened or ready PR: extracts Jira links and posts alert to Slack.

    Args:
        repo: Repository identifier.
        pr_number: Pull request number.
        pr_title: Pull request title.
        pr_author: PR author.
        pr_url: Web URL to PR.
        head_ref: Source branch.
        base_ref: Target branch.
        pr_body: Description or body of the PR.
        slack_client: Optional pre-configured SlackClient.
        jira_client: Optional pre-configured JiraClient.
        channel_id: Optional target Slack channel.

    Returns:
        Dict with status, message ts, and detected Jira key.
    """
    settings = get_settings()
    target_channel = channel_id or settings.slack_channel_id

    # 1. Extract Jira keys from title, branch, and body
    combined_text = f"{pr_title} {head_ref} {pr_body}"
    jira_keys = extract_jira_keys(combined_text)
    detected_key = jira_keys[0] if jira_keys else None

    # 2. Fetch Jira ticket details if linked
    jira_info: Optional[Dict[str, Any]] = None
    if detected_key:
        jira_info = await fetch_jira_ticket_context(detected_key, jira_client=jira_client)

    # 3. Build Block Kit message
    blocks = build_pr_slack_blocks(
        repo=repo,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_author=pr_author,
        pr_url=pr_url,
        head_ref=head_ref,
        base_ref=base_ref,
        jira_info=jira_info,
    )

    # 4. Post message to Slack
    client = slack_client or SlackClient()
    try:
        res = await client.post_message(
            channel=target_channel,
            text=f"New Pull Request #{pr_number} in {repo} ready for review: {pr_title}",
            blocks=blocks,
        )
        return {
            "status": "posted",
            "ts": res.get("ts"),
            "channel": target_channel,
            "jira_key": detected_key,
        }
    finally:
        if slack_client is None:
            await client.close()


def _record_action(
    session_factory: Optional[Any],
    action_type: str,
    target_id: str,
    user_id: str,
    jira_key: Optional[str],
    action_id: str,
    status: str,
    result: str,
) -> None:
    """Record an audit log entry in the actions_taken table."""
    try:
        factory = session_factory or get_session_factory()
        with factory() as session:
            action_record = ActionTaken(
                workspace_id="default",
                action_type=action_type,
                target_id=target_id,
                payload=json.dumps({"user_id": user_id, "jira_key": jira_key, "action_id": action_id}),
                status=status,
                approved_by=user_id,
                result=result,
                executed_at=utc_now(),
                created_at=utc_now(),
            )
            session.add(action_record)
            session.commit()
    except Exception as audit_err:
        logger.warning("Could not record ActionTaken for %s (%s): %s", action_type, target_id, audit_err)


async def handle_pr_approval_action(
    action_id: str,
    value: str,
    user_id: str,
    dry_run: Optional[bool] = None,
    github_client: Optional[GitHubClient] = None,
    jira_client: Optional[JiraClient] = None,
    session_factory: Optional[Any] = None,
) -> str:
    """Execute Approve & Merge or Decline action on GitHub and Jira.

    Args:
        action_id: 'pr_approve_and_merge' or 'pr_decline'.
        value: Serialized value 'repo#pr_number|jira_key' or 'repo#pr_number'.
        user_id: Slack user ID of approver.
        dry_run: Optional dry_run override. Defaults to settings.dry_run_actions.
        github_client: Optional pre-configured GitHubClient.
        jira_client: Optional pre-configured JiraClient.
        session_factory: Optional DB session factory for audit log.

    Returns:
        Status description string for Slack card resolution.
    """
    settings = get_settings()
    is_dry_run = dry_run if dry_run is not None else settings.dry_run_actions

    # Parse repo, PR number, and Jira key
    parts = value.split("|")
    pr_spec = parts[0].strip()
    jira_key = parts[1].strip() if len(parts) > 1 else None

    match = re.match(r"^([^#]+)#(\d+)$", pr_spec)
    if not match:
        raise ValueError(f"Invalid PR spec in action value: {value}")
    repo, str_num = match.groups()
    pull_number = int(str_num)

    is_approve = action_id == "pr_approve_and_merge"

    logger.info(
        "Handling PR action '%s' for %s#%d (user=%s, dry_run=%s, jira=%s)",
        action_id,
        repo,
        pull_number,
        user_id,
        is_dry_run,
        jira_key,
    )

    gh_client: Optional[GitHubClient] = None
    j_client: Optional[JiraClient] = None

    if not is_dry_run:
        gh_client = github_client or GitHubClient()
        if jira_key:
            j_client = jira_client or JiraClient()

    try:
        if is_approve:
            # Enforce approver permissions
            raw_approvers = settings.slack_approver_ids
            if isinstance(raw_approvers, str):
                approver_ids = [uid.strip() for uid in raw_approvers.split(",") if uid.strip()]
            else:
                approver_ids = [str(uid).strip() for uid in (raw_approvers or []) if str(uid).strip()]

            if not approver_ids:
                msg = (
                    ":warning: Merge refused: `SLACK_APPROVER_IDS` is empty. "
                    "Please configure `SLACK_APPROVER_IDS` in your `.env` (e.g. `SLACK_APPROVER_IDS=U12345,U67890`) to designate authorized approvers."
                )
                logger.warning("Merge refused for %s#%d: SLACK_APPROVER_IDS is empty", repo, pull_number)
                _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                return msg

            if user_id not in approver_ids:
                msg = f":warning: Merge refused: <@{user_id}> is not authorized to merge pull requests (not listed in `SLACK_APPROVER_IDS`)."
                logger.warning("Merge refused for %s#%d: unauthorized user %s", repo, pull_number, user_id)
                _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                return msg

            if is_dry_run:
                msg = f"[DRY RUN] Would merge {repo}#{pull_number} to main"
                if jira_key:
                    msg += f" and mark Jira {jira_key} as Done"
                status_str = "dry_run"
            else:
                assert gh_client is not None

                # Safety check 1: PR state and mergeable status
                try:
                    pr_data = await gh_client.get_pull_request(repo=repo, pull_number=pull_number)
                except Exception as exc:
                    logger.warning("Could not fetch PR details for safety checks: %s", exc)
                    pr_data = None

                if not isinstance(pr_data, dict):
                    pr_data = {"state": "open", "merged": False, "mergeable": True, "head": {"sha": ""}}

                if bool(pr_data.get("merged", False)):
                    msg = f":warning: Merge refused: Pull request {repo}#{pull_number} is already merged."
                    logger.warning(msg)
                    _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                    return msg

                pr_state = str(pr_data.get("state", "open")).lower()
                if pr_state != "open":
                    msg = f":warning: Merge refused: Pull request {repo}#{pull_number} is not open (current state: {pr_state})."
                    logger.warning(msg)
                    _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                    return msg

                mergeable = pr_data.get("mergeable")
                mergeable_state = str(pr_data.get("mergeable_state", "")).lower()
                if mergeable is False or mergeable_state in ("dirty", "conflict"):
                    msg = f":warning: Merge refused: Pull request {repo}#{pull_number} has merge conflicts and is not mergeable."
                    logger.warning(msg)
                    _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                    return msg

                # Safety check 2: CI status check
                head_sha = ""
                if isinstance(pr_data.get("head"), dict):
                    head_sha = str(pr_data["head"].get("sha", "")).strip()

                status_data: Dict[str, Any] = {}
                check_runs_data: Dict[str, Any] = {}
                if head_sha:
                    try:
                        s_res = await gh_client.get_commit_status(repo=repo, sha=head_sha)
                        if isinstance(s_res, dict):
                            status_data = s_res
                    except Exception as s_err:
                        logger.debug("Could not get commit status for %s@%s: %s", repo, head_sha, s_err)

                    try:
                        c_res = await gh_client.get_commit_check_runs(repo=repo, sha=head_sha)
                        if isinstance(c_res, dict):
                            check_runs_data = c_res
                    except Exception as c_err:
                        logger.debug("Could not get check runs for %s@%s: %s", repo, head_sha, c_err)

                status_count = status_data.get("total_count", 0)
                check_runs = check_runs_data.get("check_runs", [])
                check_count = check_runs_data.get("total_count", len(check_runs))

                ci_note = ""
                if status_count == 0 and check_count == 0:
                    ci_note = " (no CI found)"
                else:
                    if status_count > 0:
                        comb_state = str(status_data.get("state", "")).lower()
                        if comb_state not in ("success", ""):
                            msg = f":warning: Merge refused: CI status is {comb_state} for commit {head_sha[:7] if head_sha else 'head'}."
                            logger.warning(msg)
                            _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                            return msg

                    if check_count > 0:
                        for cr in check_runs:
                            cr_name = cr.get("name", "check")
                            cr_status = str(cr.get("status", "")).lower()
                            cr_conclusion = str(cr.get("conclusion") or "").lower()

                            if cr_status != "completed":
                                msg = f":warning: Merge refused: CI check run '{cr_name}' is still in progress ({cr_status})."
                                logger.warning(msg)
                                _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                                return msg
                            if cr_conclusion not in ("success", "neutral", "skipped"):
                                msg = f":warning: Merge refused: CI check run '{cr_name}' failed ({cr_conclusion})."
                                logger.warning(msg)
                                _record_action(session_factory, "pr_approve_and_merge", f"{repo}#{pull_number}", user_id, jira_key, action_id, "refused", msg)
                                return msg

                # All safety checks passed -> Proceed to merge
                merge_res = await gh_client.merge_pull_request(
                    repo=repo,
                    pull_number=pull_number,
                    commit_title=f"Merge pull request #{pull_number} from {repo}",
                    commit_message=f"Approved and merged by @{user_id} via TeamPulse Slack action.",
                )
                sha_str = ""
                if isinstance(merge_res, dict) and merge_res.get("sha"):
                    sha_str = f" (SHA: {merge_res['sha'][:7]})"
                msg = f"Merged {repo}#{pull_number} to main{sha_str}{ci_note}"

                if jira_key and j_client is not None:
                    try:
                        from datetime import datetime
                        now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
                        transitioned_status = await j_client.mark_issue_done(jira_key)
                        comment_note = (
                            f"TeamPulse: Pull request {repo}#{pull_number} was approved and merged to main "
                            f"by Slack user <@{user_id}> at {now_str}."
                        )
                        if transitioned_status:
                            comment_note += f" Ticket transitioned to {transitioned_status}."
                            msg += f" & marked Jira {jira_key} as {transitioned_status} ({now_str})"
                        else:
                            msg += f" & updated Jira {jira_key} ({now_str})"
                        await j_client.add_comment(issue_key=jira_key, comment_text=comment_note)

                        # Record TeamPulse's Jira auto-done transition in actions_taken
                        _record_action(
                            session_factory,
                            action_type="jira_auto_done",
                            target_id=jira_key,
                            user_id=user_id,
                            jira_key=jira_key,
                            action_id="jira_auto_done",
                            status="executed",
                            result=f"Moved {jira_key} to Done after merging {repo}#{pull_number}",
                        )
                    except Exception as j_err:
                        logger.warning("Failed to update/transition Jira issue %s: %s", jira_key, j_err)

                status_str = "executed"

            action_type = "pr_approve_and_merge"

        else:
            if is_dry_run:
                msg = f"[DRY RUN] Would decline and close {repo}#{pull_number}"
                if jira_key:
                    msg += f" and notify Jira {jira_key}"
            else:
                assert gh_client is not None
                await gh_client.close_pull_request(repo=repo, pull_number=pull_number)
                msg = f"Declined and closed {repo}#{pull_number}"
                if jira_key and j_client is not None:
                    try:
                        await j_client.add_comment(
                            issue_key=jira_key,
                            comment_text=(
                                f"TeamPulse: Pull request {repo}#{pull_number} was declined "
                                f"by Slack user <@{user_id}>."
                            ),
                        )
                        msg += f" & updated Jira {jira_key}"
                    except Exception as j_err:
                        logger.warning("Failed to comment on Jira issue %s: %s", jira_key, j_err)

            status_str = "dry_run" if is_dry_run else "executed"
            action_type = "pr_decline"

        # Record audit log in actions_taken
        _record_action(
            session_factory,
            action_type=action_type,
            target_id=f"{repo}#{pull_number}",
            user_id=user_id,
            jira_key=jira_key,
            action_id=action_id,
            status=status_str,
            result=msg,
        )

        return msg

    finally:
        if gh_client is not None and github_client is None:
            await gh_client.close()
        if j_client is not None and jira_client is None:
            await j_client.close()
