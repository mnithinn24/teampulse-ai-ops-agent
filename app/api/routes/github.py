"""GitHub webhook and PR notification routes (Phase 9)."""

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.agents.pr_alert import notify_new_pull_request
from app.api.routes.briefing import verify_admin_api_key
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github", tags=["github"])


def verify_github_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    secret: str,
) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    Args:
        raw_body: Raw bytes of request body.
        signature_header: Value of 'X-Hub-Signature-256' header (e.g. 'sha256=...').
        secret: Configured GITHUB_WEBHOOK_SECRET.

    Returns:
        True if signature is valid or secret is empty, False otherwise.
    """
    if not secret:
        # If no secret configured, allow the request
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = signature_header.split("sha256=")[-1]
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, expected_signature)


class PRAlertRequest(BaseModel):
    """Payload for manually triggering a PR alert to Slack."""

    repo: str = Field(..., description="Repository in 'owner/repo' format")
    pr_number: int = Field(..., description="Pull request number")
    title: Optional[str] = Field(default=None, description="PR title (e.g. '[KAN-42] Implement Medical RAG')")
    author: Optional[str] = Field(default=None, description="PR author GitHub username")
    url: Optional[str] = Field(default=None, description="PR web URL")
    head_ref: Optional[str] = Field(default="feature", description="Head branch name")
    base_ref: Optional[str] = Field(default="main", description="Target base branch name")
    body: Optional[str] = Field(default="", description="PR description body containing Jira key")
    channel_id: Optional[str] = Field(default=None, description="Optional target Slack channel ID")


@router.post("/webhook")
async def handle_github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Handle incoming GitHub webhook events with HMAC-SHA256 verification.

    Triggers Slack alert with [Approve & Merge] / [Decline] buttons when a PR is opened.
    """
    settings = get_settings()
    raw_body = await request.body()

    # 1. Verify webhook signature if secret configured
    if settings.github_webhook_secret:
        valid = verify_github_signature(
            raw_body=raw_body,
            signature_header=x_hub_signature_256,
            secret=settings.github_webhook_secret,
        )
        if not valid:
            logger.warning("Rejected GitHub webhook with invalid signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid GitHub webhook signature",
            )

    # 2. Check event type
    event = (x_github_event or "").lower()
    if event != "pull_request":
        logger.info("Ignoring non-PR GitHub event: %s", event)
        return {"status": "ignored", "event": event}

    # 3. Parse JSON body
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        logger.error("Malformed GitHub webhook JSON: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload",
        ) from exc

    action = payload.get("action", "")
    if action not in ("opened", "reopened", "ready_for_review"):
        logger.info("Ignoring PR action '%s'", action)
        return {"status": "ignored", "action": action}

    pr = payload.get("pull_request", {})
    repo_data = payload.get("repository", {})
    repo = repo_data.get("full_name") or repo_data.get("name", "unknown/repo")
    pr_number = pr.get("number", 0)
    pr_title = pr.get("title", f"Pull Request #{pr_number}")
    user_data = pr.get("user", {})
    pr_author = user_data.get("login", "unknown")
    pr_url = pr.get("html_url", f"https://github.com/{repo}/pull/{pr_number}")
    head_ref = pr.get("head", {}).get("ref", "feature")
    base_ref = pr.get("base", {}).get("ref", "main")
    pr_body = pr.get("body") or ""

    logger.info("Dispatching Slack PR alert for %s#%d (%s)", repo, pr_number, pr_title)

    background_tasks.add_task(
        notify_new_pull_request,
        repo=repo,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_author=pr_author,
        pr_url=pr_url,
        head_ref=head_ref,
        base_ref=base_ref,
        pr_body=pr_body,
    )

    return {
        "status": "ok",
        "message": f"PR alert queued for {repo}#{pr_number}",
        "action": action,
    }


@router.post("/pr/alert")
@router.post("/alert")
async def trigger_manual_pr_alert(
    body: PRAlertRequest,
    _: str = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Manually trigger a PR completion Slack alert with Jira links and Approve/Decline buttons.

    Requires X-API-Key header.
    """
    title = body.title or f"Pull Request #{body.pr_number}"
    author = body.author or "developer"
    url = body.url or f"https://github.com/{body.repo}/pull/{body.pr_number}"

    try:
        res = await notify_new_pull_request(
            repo=body.repo,
            pr_number=body.pr_number,
            pr_title=title,
            pr_author=author,
            pr_url=url,
            head_ref=body.head_ref or "feature",
            base_ref=body.base_ref or "main",
            pr_body=body.body or "",
            channel_id=body.channel_id,
        )
        return res
    except Exception as exc:
        err_msg = str(exc)
        if "not_in_channel" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Slack bot is not a member of channel '{body.channel_id or 'default'}'. "
                    "Please invite the bot in Slack by typing: /invite @teampulse2 into the channel."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to post PR alert to Slack: {err_msg}",
        ) from exc


class PRMergeRequest(BaseModel):
    """Payload for triggering one-click PR auto-merge and Jira Done transition."""

    repo: str = Field(..., description="Repository in 'owner/repo' format")
    pr_number: int = Field(..., description="Pull request number")
    jira_key: Optional[str] = Field(default=None, description="Optional linked Jira issue key (e.g. 'KAN-42')")
    user_id: Optional[str] = Field(default="api_user", description="Approver user ID")
    dry_run: Optional[bool] = Field(default=None, description="Optional dry-run override flag")


@router.post("/pr/merge")
async def trigger_pr_merge_endpoint(
    body: PRMergeRequest,
    _: str = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Execute one-click PR auto-merge on GitHub and mark linked Jira ticket as Done.

    Requires X-API-Key header.
    """
    from app.agents.pr_alert import handle_pr_approval_action

    composite_val = f"{body.repo}#{body.pr_number}"
    if body.jira_key:
        composite_val += f"|{body.jira_key}"

    result_msg = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value=composite_val,
        user_id=body.user_id or "api_user",
        dry_run=body.dry_run,
    )

    return {
        "status": "success",
        "result": result_msg,
        "repo": body.repo,
        "pr_number": body.pr_number,
        "jira_key": body.jira_key,
    }
