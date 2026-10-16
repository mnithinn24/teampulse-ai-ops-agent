import asyncio
from collections import defaultdict
import json
import logging
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.actions import execute_approved_action
from app.agents.query import handle_plain_language_query
from app.config import get_settings
from app.core.constants import ITEM_TYPE_JIRA_ISSUE
from app.core.security import verify_slack_signature
from app.db.database import get_db_session, get_session_factory
from app.db.models import ActionTaken, AgentRun, Flag, utc_now
from app.graph.runner import is_workspace_running, trigger_pipeline_run
from app.integrations.jira_client import JiraClient
from app.integrations.slack_client import SlackClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])


async def send_ephemeral_message(
    response_url: Optional[str],
    channel_id: Optional[str],
    user_id: str,
    text: str,
    slack_client: Optional[SlackClient] = None,
) -> None:
    """Send an ephemeral message visible only to the specified user."""
    if response_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    response_url,
                    json={"response_type": "ephemeral", "text": text},
                )
                if res.is_success:
                    return
        except Exception as exc:
            logger.warning("Failed to post ephemeral via response_url: %s", exc)

    if channel_id:
        try:
            client = slack_client or SlackClient()
            await client.post_ephemeral(channel=channel_id, user=user_id, text=text)
        except Exception as exc:
            logger.warning("Failed to post ephemeral via SlackClient: %s", exc)


def replace_action_buttons_in_blocks(
    blocks: List[Dict[str, Any]],
    flag_id: int,
    status_text: str,
) -> List[Dict[str, Any]]:
    """Replace the action buttons block for a specific flag with a resolution context block.

    Args:
        blocks: List of Slack Block Kit blocks from the original message.
        flag_id: Database ID of the target flag.
        status_text: Resolution text (e.g. 'Approved by <@U123> - created KAN-12').

    Returns:
        New updated list of blocks.
    """
    new_blocks = []
    target_block_id = f"flag_actions_{flag_id}"
    str_flag_id = str(flag_id)

    for block in blocks:
        block_id = block.get("block_id", "")
        is_target_actions = False

        if block_id == target_block_id:
            is_target_actions = True
        elif block.get("type") == "actions":
            elements = block.get("elements", [])
            for elem in elements:
                if elem.get("value") == str_flag_id:
                    is_target_actions = True
                    break

        if is_target_actions:
            # Replace actions block with resolution context block
            new_blocks.append({
                "type": "context",
                "block_id": f"flag_resolution_{flag_id}",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": status_text,
                    }
                ],
            })
        else:
            new_blocks.append(block)

    return new_blocks


async def process_slack_interaction(
    payload: Dict[str, Any],
    session_factory: Optional[Any] = None,
    jira_client: Optional[JiraClient] = None,
    slack_client: Optional[SlackClient] = None,
) -> None:
    """Process Slack interaction payload in the background.

    Idempotently handles Approve and Dismiss button clicks.
    """
    settings = get_settings()
    user = payload.get("user", {})
    user_id = user.get("id", "unknown_user")
    actions = payload.get("actions", [])
    response_url = payload.get("response_url")
    channel = payload.get("channel", {})
    channel_id = channel.get("id")
    message = payload.get("message", {})
    message_ts = message.get("ts")
    blocks = message.get("blocks", [])

    if not actions:
        logger.warning("No actions found in Slack interaction payload.")
        return

    action = actions[0]
    action_id = action.get("action_id")
    raw_value = action.get("value")

    try:
        flag_id = int(raw_value)
    except (ValueError, TypeError):
        logger.error("Invalid flag ID in action value: %s", raw_value)
        return

    # 1. Check approver authorization
    approvers = settings.slack_approver_ids
    if approvers and user_id not in approvers:
        logger.warning("Unauthorized user %s attempted action %s on flag %s", user_id, action_id, flag_id)
        await send_ephemeral_message(
            response_url=response_url,
            channel_id=channel_id,
            user_id=user_id,
            text=f":warning: <@{user_id}>, you are not authorized to approve or dismiss items.",
            slack_client=slack_client,
        )
        return

    is_approve = action_id == "approve_action"
    target_status = "approved" if is_approve else "dismissed"

    # 2. Atomic check-and-update to ensure idempotency against double-clicks
    factory = session_factory or get_session_factory()
    with factory() as session:
        # Atomic conditional update: only transitions if status is currently 'pending'
        stmt = (
            update(Flag)
            .where(Flag.id == flag_id, Flag.status == "pending")
            .values(
                status=target_status,
                approved_by=user_id,
                decided_at=utc_now(),
            )
        )
        result = session.execute(stmt)
        session.commit()

        if result.rowcount == 0:
            # Item was already decided
            logger.info("Flag %s was already decided. Returning ephemeral notice to %s.", flag_id, user_id)
            await send_ephemeral_message(
                response_url=response_url,
                channel_id=channel_id,
                user_id=user_id,
                text=f":information_source: Item #{flag_id} has already been handled.",
                slack_client=slack_client,
            )
            return

        # Fetch the updated flag model
        flag = session.get(Flag, flag_id)
        if not flag:
            return

        # 3. Handle Dismiss
        if not is_approve:
            status_text = f":x: *Dismissed by <@{user_id}>*"
            action_record = ActionTaken(
                workspace_id=flag.workspace_id,
                action_type="dismiss_flag",
                target_id=flag.item_id,
                payload=flag.suggested_action,
                status="dismissed",
                approved_by=user_id,
                result="Dismissed without action",
                executed_at=utc_now(),
                created_at=utc_now(),
            )
            session.add(action_record)
            session.commit()

            # Update Slack message to replace buttons
            if channel_id and message_ts:
                updated_blocks = replace_action_buttons_in_blocks(blocks, flag_id, status_text)
                client = slack_client or SlackClient()
                try:
                    await client.update_message(
                        channel=channel_id,
                        ts=message_ts,
                        text=f"Item #{flag.item_id} dismissed by <@{user_id}>",
                        blocks=updated_blocks,
                    )
                except Exception as exc:
                    logger.error("Failed to update Slack message on dismiss: %s", exc)
            return

        # 4. Handle Approve -> Run Action Executor
        action_type = "jira_comment" if flag.item_type in (ITEM_TYPE_JIRA_ISSUE, "jira_ticket") else "jira_create_ticket"
        is_dry_run = settings.dry_run_actions
        try:
            exec_result = await execute_approved_action(flag, jira_client=jira_client)
            flag.result = exec_result

            if exec_result.startswith("Already tracked as"):
                status_text = f":information_source: {exec_result}"
                action_status = "skipped_duplicate"
            else:
                status_text = f":white_check_mark: *Approved by <@{user_id}>* — {exec_result}"
                action_status = "dry_run" if is_dry_run else "executed"
            action_record = ActionTaken(
                workspace_id=flag.workspace_id,
                action_type=action_type,
                target_id=flag.item_id,
                payload=flag.suggested_action,
                status=action_status,
                approved_by=user_id,
                result=exec_result,
                executed_at=utc_now(),
                created_at=utc_now(),
            )
            session.add(action_record)
            session.commit()

        except Exception as exc:
            logger.error("Action execution failed for flag %s: %s", flag_id, exc)
            flag.status = "failed"
            flag.result = str(exc)
            status_text = f":warning: *Approved by <@{user_id}>, but action failed:* {exc}"

            action_record = ActionTaken(
                workspace_id=flag.workspace_id,
                action_type=action_type,
                target_id=flag.item_id,
                payload=flag.suggested_action,
                status="failed",
                approved_by=user_id,
                error=str(exc),
                executed_at=utc_now(),
                created_at=utc_now(),
            )
            session.add(action_record)
            session.commit()

        # Update Slack message to replace buttons with resolution text
        if channel_id and message_ts:
            updated_blocks = replace_action_buttons_in_blocks(blocks, flag_id, status_text)
            client = slack_client or SlackClient()
            try:
                await client.update_message(
                    channel=channel_id,
                    ts=message_ts,
                    text=f"Item #{flag.item_id}: {status_text}",
                    blocks=updated_blocks,
                )
            except Exception as exc:
                logger.error("Failed to update Slack message on approval: %s", exc)


@router.post("/interactions")
async def handle_slack_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(None),
    x_slack_signature: Optional[str] = Header(None),
) -> Response:
    """Handle incoming Slack interactive button clicks with signature verification.

    Responses are acknowledged immediately (<3s) and processed asynchronously
    via background tasks.
    """
    raw_body = await request.body()

    # 1. Verify HMAC signature and timestamp
    is_valid = verify_slack_signature(
        raw_body=raw_body,
        timestamp=x_slack_request_timestamp,
        signature=x_slack_signature,
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack request signature or expired timestamp",
        )

    # 2. Parse form-encoded payload
    try:
        body_str = raw_body.decode("utf-8")
        parsed_form = urllib.parse.parse_qs(body_str)
        payload_raw = parsed_form.get("payload", [""])[0]
        if not payload_raw:
            raise ValueError("Missing payload parameter")
        payload = json.loads(payload_raw)
    except Exception as exc:
        logger.error("Failed to parse Slack interaction payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed interaction payload",
        ) from exc

    # 3. Schedule background processing to answer Slack within 3 seconds
    background_tasks.add_task(process_slack_interaction, payload)

    return Response(content="", status_code=status.HTTP_200_OK)


# -----------------------------------------------------------------------------
# Slash Command & Query Rate Limiting
# -----------------------------------------------------------------------------
_user_query_timestamps: Dict[str, List[float]] = defaultdict(list)
_rate_limiter_lock = asyncio.Lock()


async def check_user_rate_limit(user_id: str, limit_per_minute: int) -> bool:
    """Sliding-window rate limiter per Slack user ID for queries.

    Args:
        user_id: Slack user ID string.
        limit_per_minute: Max allowable queries in a 60-second window.

    Returns:
        True if the request is permitted, False if limit is exceeded.
    """
    async with _rate_limiter_lock:
        now = time.time()
        cutoff = now - 60.0
        active_ts = [ts for ts in _user_query_timestamps[user_id] if ts >= cutoff]
        if len(active_ts) >= limit_per_minute:
            _user_query_timestamps[user_id] = active_ts
            return False
        active_ts.append(now)
        _user_query_timestamps[user_id] = active_ts
        return True


def reset_user_rate_limits() -> None:
    """Clear in-memory user rate limits (useful for testing)."""
    _user_query_timestamps.clear()


async def _post_command_response(response_url: str, payload: Dict[str, Any]) -> None:
    """Post an asynchronous response to Slack's command response_url."""
    if not response_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(response_url, json=payload)
    except Exception as exc:
        logger.warning("Failed to post async response to %s: %s", response_url, exc)


async def _run_slash_briefing_background(response_url: str) -> None:
    """Run briefing pipeline in background and post confirmation to Slack."""
    try:
        run_id, _ = await trigger_pipeline_run(workspace_id="default", run_type="ondemand", background=False)
        await _post_command_response(
            response_url,
            {
                "response_type": "in_channel",
                "text": f":white_check_mark: TeamPulse briefing run #{run_id} completed successfully!",
            },
        )
    except Exception as exc:
        logger.error("Background slash briefing failed: %s", exc)
        await _post_command_response(
            response_url,
            {
                "response_type": "ephemeral",
                "text": f":x: Briefing run encountered an error: {exc}",
            },
        )


async def _run_slash_query_background(question: str, response_url: str) -> None:
    """Run plain-language query in background and post ephemeral answer to Slack."""
    try:
        res = await handle_plain_language_query(question=question, workspace_id="default")
        answer = res.get("answer", "No answer found.")
        sources = ", ".join(res.get("sources_used", [])) or "None"
        formatted_text = (
            f"*Question:* {question}\n"
            f"*Answer:* {answer}\n"
            f"_Sources: {sources} | Model: {res.get('model_used')} | Latency: {res.get('latency_ms', 0):.0f}ms_"
        )
        await _post_command_response(
            response_url,
            {
                "response_type": "ephemeral",
                "text": formatted_text,
            },
        )
    except Exception as exc:
        logger.error("Background slash query failed: %s", exc)
        await _post_command_response(
            response_url,
            {
                "response_type": "ephemeral",
                "text": f":x: Failed to answer question: {exc}",
            },
        )


@router.post("/commands")
async def handle_slack_commands(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(None),
    x_slack_signature: Optional[str] = Header(None),
) -> Response:
    """Handle incoming Slack slash command /teampulse with signature verification.

    Subcommands:
    - /teampulse brief : Trigger on-demand briefing (guarded by concurrency lock)
    - /teampulse ask <question> : Ask natural language question (rate-limited per user)
    - /teampulse status : Display status and metrics of last run
    - /teampulse help : Show available commands
    """
    raw_body = await request.body()

    # 1. Verify HMAC signature & timestamp
    is_valid = verify_slack_signature(
        raw_body=raw_body,
        timestamp=x_slack_request_timestamp,
        signature=x_slack_signature,
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack request signature or expired timestamp",
        )

    # 2. Parse form body
    body_str = raw_body.decode("utf-8")
    parsed_form = urllib.parse.parse_qs(body_str)
    command = parsed_form.get("command", [""])[0]
    text = parsed_form.get("text", [""])[0].strip()
    user_id = parsed_form.get("user_id", ["anonymous"])[0]
    response_url = parsed_form.get("response_url", [""])[0]

    logger.info("Received slash command %s text='%s' from user %s", command, text, user_id)

    # 3. Handle subcommands
    text_lower = text.lower()

    if text_lower == "brief":
        # Concurrency check
        if is_workspace_running("default"):
            return Response(
                content=json.dumps({
                    "response_type": "ephemeral",
                    "text": "A briefing run is already in progress for this workspace. Please wait.",
                }),
                media_type="application/json",
                status_code=status.HTTP_200_OK,
            )

        background_tasks.add_task(_run_slash_briefing_background, response_url)
        return Response(
            content=json.dumps({
                "response_type": "ephemeral",
                "text": ":runner: Briefing run started in background. Results will be posted here.",
            }),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )

    elif text_lower.startswith("ask"):
        question = text[3:].strip()
        if not question:
            return Response(
                content=json.dumps({
                    "response_type": "ephemeral",
                    "text": "Please provide a question. Example: `/teampulse ask which PRs are stale?`",
                }),
                media_type="application/json",
                status_code=status.HTTP_200_OK,
            )

        settings = get_settings()
        under_limit = await check_user_rate_limit(user_id, settings.query_rate_limit_per_minute)
        if not under_limit:
            return Response(
                content=json.dumps({
                    "response_type": "ephemeral",
                    "text": f"Rate limit exceeded: maximum {settings.query_rate_limit_per_minute} questions per minute. Please try again later.",
                }),
                media_type="application/json",
                status_code=status.HTTP_200_OK,
            )

        background_tasks.add_task(_run_slash_query_background, question, response_url)
        return Response(
            content=json.dumps({
                "response_type": "ephemeral",
                "text": f":thinking_face: Asking TeamPulse: \"{question}\"...",
            }),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )

    elif text_lower == "status":
        with get_db_session() as session:
            latest = session.scalars(select(AgentRun).order_by(AgentRun.id.desc())).first()
            if not latest:
                status_text = "No TeamPulse runs found yet."
            else:
                last_time = (
                    latest.completed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    if latest.completed_at
                    else (latest.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if latest.created_at else "In progress")
                )
                lat_str = f"{latest.total_latency_ms:.1f}ms" if latest.total_latency_ms is not None else "N/A"
                status_text = (
                    f":information_source: *TeamPulse Latest Run Status:*\n"
                    f"• *Status:* {latest.status.upper()}\n"
                    f"• *Run Type:* {latest.run_type}\n"
                    f"• *Last Run:* {last_time}\n"
                    f"• *Model Used:* {latest.model_used or 'N/A'}\n"
                    f"• *Items Collected:* {latest.items_collected or 0} | *Flags Raised:* {latest.flags_created or 0}\n"
                    f"• *Total Latency:* {lat_str}"
                )

        return Response(
            content=json.dumps({
                "response_type": "ephemeral",
                "text": status_text,
            }),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )

    else:
        # Help text
        help_text = (
            ":wave: *TeamPulse Ops Agent Commands:*\n"
            "• `/teampulse brief` — Trigger a fresh briefing run\n"
            "• `/teampulse ask <question>` — Ask an operational question about PRs or tickets\n"
            "• `/teampulse status` — Display status and metrics of the latest run\n"
            "• `/teampulse help` — Show this help message"
        )
        return Response(
            content=json.dumps({
                "response_type": "ephemeral",
                "text": help_text,
            }),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )

