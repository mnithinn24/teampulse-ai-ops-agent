import hmac
import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.routes.briefing import verify_admin_api_key
from app.config import get_settings
from app.db.database import get_session_factory
from app.db.models import ActionTaken, utc_now
from app.integrations.jira_client import JiraClient
from app.integrations.slack_client import SlackClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jira", tags=["jira"])


def resolve_pr_url_for_issue(issue_key: str, custom_pr_url: Optional[str] = None) -> str:
    """Resolve the Pull Request URL for reviewing code commits and changes."""
    if custom_pr_url:
        return custom_pr_url

    import re
    settings = get_settings()
    configured_base = getattr(settings, "pr_base_url", "").strip()
    if configured_base:
        if "{issue_key}" in configured_base:
            return configured_base.format(issue_key=issue_key)
        # Direct link to an existing PR (e.g. Bitbucket /pull-requests/224 or GitHub /pull/1)
        if re.search(r"/pull(-requests)?/\d+", configured_base):
            return configured_base
        if "bitbucket.org" in configured_base.lower():
            return f"{configured_base.rstrip('/')}?q={issue_key}"
        if "github.com" in configured_base.lower():
            if "/pulls" in configured_base:
                return f"{configured_base.rstrip('/')}?q=is%3Apr+{issue_key}"
            return f"{configured_base.rstrip('/')}/pulls?q=is%3Apr+{issue_key}"
        return configured_base

    primary_repo = settings.github_repos[0] if settings.github_repos else "mnithinn24/teampulse-ai-ops-agent"
    return f"https://github.com/{primary_repo}/pulls?q=is%3Apr+{issue_key}"


def build_jira_task_slack_blocks(
    issue_key: str,
    title: str,
    status_name: str,
    assignee: str,
    issue_url: str,
    pr_url: Optional[str] = None,
    description: str = "",
) -> List[Dict[str, Any]]:
    """Build rich Slack Block Kit layout for completed Jira task review with PR and Jira links."""
    resolved_pr_url = resolve_pr_url_for_issue(issue_key, pr_url)

    desc_preview = description.strip()
    if len(desc_preview) > 300:
        desc_preview = desc_preview[:297] + "..."
    desc_line = f"\n• *Details:* {desc_preview}" if desc_preview else ""

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🎯 Task Completed — Ready for Code Review",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*🔍 Pull Request (Inspect Commits & Files Changed):*\n"
                    f"• :git_pull_request: <{resolved_pr_url}|*Open Pull Request for {issue_key} (Review Commits & Diff)* ↗>\n\n"
                    f"*Work Item Details:*\n"
                    f"• *Jira Ticket:* <{issue_url}|*{issue_key}: {title}* ↗>\n"
                    f"• *Assignee:* `@{assignee}`\n"
                    f"• *Status:* *{status_name}*"
                    f"{desc_line}"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"jira_actions_{issue_key}",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔍 Open Pull Request ↗",
                        "emoji": True,
                    },
                    "url": resolved_pr_url,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Approve & Verify",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "jira_approve_task",
                    "value": issue_key,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Decline & Reopen",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": "jira_decline_task",
                    "value": issue_key,
                },
            ],
        },
    ]
    return blocks


async def notify_jira_task_completion(
    issue_key: str,
    title: Optional[str] = None,
    status_name: Optional[str] = None,
    assignee: Optional[str] = None,
    description: Optional[str] = None,
    channel_id: Optional[str] = None,
    jira_client: Optional[JiraClient] = None,
    slack_client: Optional[SlackClient] = None,
) -> Dict[str, Any]:
    """Fetch Jira issue details (if needed) and post task review alert into Slack."""
    settings = get_settings()
    target_channel = channel_id or settings.slack_channel_id

    # If details omitted, fetch live from Jira Cloud
    j_client = jira_client or JiraClient()
    try:
        if not title or not status_name or not assignee:
            issue = await j_client.get_issue(issue_key)
            issue_title = title or issue.title
            issue_status = status_name or issue.status
            issue_assignee = assignee or issue.assignee or "Unassigned"
            issue_url = issue.url or f"{(settings.jira_base_url or '').rstrip('/')}/browse/{issue_key}"
        else:
            issue_title = title
            issue_status = status_name
            issue_assignee = assignee
            issue_url = f"{(settings.jira_base_url or '').rstrip('/')}/browse/{issue_key}"
    finally:
        if jira_client is None:
            await j_client.close()

    blocks = build_jira_task_slack_blocks(
        issue_key=issue_key,
        title=issue_title,
        status_name=issue_status,
        assignee=issue_assignee,
        issue_url=issue_url,
        description=description or "",
    )

    s_client = slack_client or SlackClient()
    try:
        res = await s_client.post_message(
            channel=target_channel,
            text=f"Jira Task {issue_key} ({issue_title}) completed by @{issue_assignee} - Ready for Lead Review",
            blocks=blocks,
        )
        return {
            "status": "posted",
            "ts": res.get("ts"),
            "channel": target_channel,
            "issue_key": issue_key,
        }
    finally:
        if slack_client is None:
            await s_client.close()


class JiraAlertRequest(BaseModel):
    """Payload for manually dispatching Jira task completion Slack alert."""

    issue_key: str = Field(..., description="Jira issue key, e.g. KAN-253")
    channel_id: Optional[str] = Field(default=None, description="Optional target Slack channel")


@router.post("/alert")
async def trigger_jira_task_alert(
    body: JiraAlertRequest,
    _: str = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Manually dispatch a Jira task completion Slack alert for Team Lead review."""
    try:
        return await notify_jira_task_completion(
            issue_key=body.issue_key,
            channel_id=body.channel_id,
        )
    except Exception as exc:
        logger.error("Failed to send Jira task alert for %s: %s", body.issue_key, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to post Jira task alert: {exc}",
        ) from exc


# In-memory registry of issue keys that have already been notified as Done
_notified_done_keys: set = set()
_poller_task: Optional[Any] = None


def is_ticket_handled_or_alerted(issue_key: str, session_factory: Optional[Any] = None) -> bool:
    """Check if a ticket was already merged/verified by TeamPulse or alerted to Slack."""
    if not issue_key:
        return False
    if issue_key in _notified_done_keys:
        return True

    try:
        factory = session_factory or get_session_factory()
        with factory() as session:
            # 1. Check if TeamPulse recorded a merge, verify, or alert action for this Jira key
            handled = (
                session.query(ActionTaken)
                .filter(
                    ActionTaken.target_id == issue_key,
                    ActionTaken.action_type.in_(["jira_auto_done", "jira_task_verified", "jira_task_alerted"]),
                    ActionTaken.status == "executed",
                )
                .first()
            )
            if handled:
                _notified_done_keys.add(issue_key)
                return True

            # 2. Check if PR merge record exists with this Jira key in payload
            pr_merge = (
                session.query(ActionTaken)
                .filter(
                    ActionTaken.action_type == "pr_approve_and_merge",
                    ActionTaken.payload.like(f"%{issue_key}%"),
                    ActionTaken.status == "executed",
                )
                .first()
            )
            if pr_merge:
                _notified_done_keys.add(issue_key)
                return True

    except Exception as exc:
        logger.warning("Error checking handled/alerted status for %s: %s", issue_key, exc)

    return False


def record_ticket_alerted(issue_key: str, session_factory: Optional[Any] = None) -> None:
    """Record in database and in-memory cache that this Jira ticket has been alerted."""
    if not issue_key:
        return
    _notified_done_keys.add(issue_key)
    try:
        factory = session_factory or get_session_factory()
        with factory() as session:
            existing = (
                session.query(ActionTaken)
                .filter(
                    ActionTaken.target_id == issue_key,
                    ActionTaken.action_type == "jira_task_alerted",
                )
                .first()
            )
            if not existing:
                record = ActionTaken(
                    workspace_id="default",
                    action_type="jira_task_alerted",
                    target_id=issue_key,
                    payload=json.dumps({"issue_key": issue_key}),
                    status="executed",
                    approved_by="system",
                    result=f"Alerted Jira task completion for {issue_key}",
                    executed_at=utc_now(),
                    created_at=utc_now(),
                )
                session.add(record)
                session.commit()
    except Exception as exc:
        logger.warning("Could not record ActionTaken for jira_task_alerted (%s): %s", issue_key, exc)


def mark_issue_reopened(issue_key: str, session_factory: Optional[Any] = None) -> None:
    """Allow a reopened ticket to trigger an alert again if marked Done later."""
    _notified_done_keys.discard(issue_key)
    try:
        factory = session_factory or get_session_factory()
        with factory() as session:
            session.query(ActionTaken).filter(
                ActionTaken.target_id == issue_key,
                ActionTaken.action_type == "jira_task_alerted",
            ).delete(synchronize_session=False)
            session.commit()
    except Exception as exc:
        logger.warning("Could not clear alerted record for reopened issue %s: %s", issue_key, exc)


@router.post("/webhook")
async def handle_jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Receive live webhook events from Jira Cloud (e.g. status transition to Done)."""
    settings = get_settings()
    expected_secret = getattr(settings, "jira_webhook_secret", "").strip()

    secret_candidate = (
        request.headers.get("X-Atlassian-Webhook-Token")
        or request.headers.get("X-Jira-Webhook-Secret")
        or request.headers.get("X-Webhook-Secret")
        or request.headers.get("token")
        or request.query_params.get("secret")
        or request.query_params.get("token")
        or ""
    )
    if not secret_candidate and "authorization" in request.headers:
        auth_val = request.headers.get("authorization", "").strip()
        if auth_val.lower().startswith("bearer "):
            secret_candidate = auth_val[7:].strip()
        else:
            secret_candidate = auth_val

    if not expected_secret or not secret_candidate or not hmac.compare_digest(secret_candidate, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing Jira webhook secret",
        )

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON") from exc

    event = payload.get("webhookEvent", "")
    issue_data = payload.get("issue", {})
    issue_key = issue_data.get("key", "")
    fields = issue_data.get("fields", {})

    status_info = fields.get("status", {})
    status_name = status_info.get("name", "")
    status_category = status_info.get("statusCategory", {}).get("key", "").lower()

    assignee_data = fields.get("assignee") or {}
    assignee = assignee_data.get("displayName") or assignee_data.get("name") or "Unassigned"
    title = fields.get("summary", "")

    # Check if the event or changelog indicates transition to Done / In Review
    is_done = (status_category == "done") or (status_name.lower() in ("done", "closed", "resolved", "in review"))

    changelog = payload.get("changelog", {})
    items = changelog.get("items", [])
    status_changed = any(item.get("field") == "status" for item in items)

    if issue_key and is_done and (status_changed or event == "jira:issue_updated"):
        # Skip tickets that have a recorded merge or verify action, or were already alerted
        if is_ticket_handled_or_alerted(issue_key):
            logger.info("Jira task %s is already handled or alerted. Skipping webhook alert.", issue_key)
            return {
                "status": "skipped",
                "issue_key": issue_key,
                "reason": "already_handled_or_alerted",
            }

        logger.info("Jira task %s transitioned to %s by %s. Dispatching Slack alert.", issue_key, status_name, assignee)
        record_ticket_alerted(issue_key)
        background_tasks.add_task(
            notify_jira_task_completion,
            issue_key=issue_key,
            title=title,
            status_name=status_name,
            assignee=assignee,
        )
        return {
            "status": "alert_queued",
            "issue_key": issue_key,
            "new_status": status_name,
        }

    return {
        "status": "ignored",
        "event": event,
        "issue_key": issue_key,
        "status_name": status_name,
    }


async def poll_jira_completed_tasks_loop(interval_seconds: float = 15.0) -> None:
    """Continuously poll Jira for tickets transitioned to Done and auto-dispatch Slack alerts."""
    import asyncio
    settings = get_settings()
    project_key = settings.jira_project_key or "KAN"
    target_channel = settings.slack_channel_id

    # Wait 3 seconds on boot before first poll
    await asyncio.sleep(3.0)
    logger.info("Jira completed tasks auto-poller started for project '%s' (interval: %ss)", project_key, interval_seconds)

    while True:
        try:
            client = JiraClient()
            try:
                jql = f"project = '{project_key}' AND status in ('Done', 'Closed', 'Resolved') ORDER BY updated DESC"
                done_issues = await client.search_issues(jql=jql, max_results=10)
                for issue in done_issues:
                    if not is_ticket_handled_or_alerted(issue.key):
                        logger.info("Auto-sync: Detected newly completed Jira task %s (%s). Posting Slack alert.", issue.key, issue.title)
                        record_ticket_alerted(issue.key)
                        await notify_jira_task_completion(
                            issue_key=issue.key,
                            title=issue.title,
                            status_name=issue.status,
                            assignee=issue.assignee or "Unassigned",
                            channel_id=target_channel,
                            jira_client=client,
                        )
                    else:
                        logger.debug("Auto-sync: Skipping %s (already handled or alerted)", issue.key)
            finally:
                await client.close()
        except asyncio.CancelledError:
            logger.info("Jira status poller task cancelled.")
            break
        except Exception as exc:
            logger.warning("Jira background status poller error: %s", exc)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


def start_jira_sync_poller() -> Optional[Any]:
    """Start the background Jira status poller task."""
    import asyncio
    global _poller_task
    if _poller_task is None or getattr(_poller_task, "done", lambda: True)():
        _poller_task = asyncio.create_task(poll_jira_completed_tasks_loop(interval_seconds=15.0))
        logger.info("Started Jira background status poller.")
    return _poller_task


def stop_jira_sync_poller() -> None:
    """Stop the background Jira status poller task."""
    global _poller_task
    if _poller_task and not getattr(_poller_task, "done", lambda: True)():
        _poller_task.cancel()
        logger.info("Stopped Jira background status poller.")
