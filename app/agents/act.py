"""Act node agent: persists pending priority flags and publishes Slack Block Kit briefing."""

from datetime import timedelta
import json
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from app.agents.briefing import build_slack_briefing
from app.config import get_settings
from app.core.constants import (
    FLAG_LEVEL_ESCALATED,
    FLAG_LEVEL_NORMAL,
    ITEM_TYPE_GITHUB_PR,
    ITEM_TYPE_JIRA_ISSUE,
    STATUS_PENDING,
    URGENCY_CRITICAL,
    URGENCY_URGENT,
    URGENCY_WATCH,
)
from app.db.database import get_session_factory
from app.db.models import AgentRun, Flag, utc_now
from app.graph.state import TeamPulseState
from app.integrations.slack_client import SlackClient

logger = logging.getLogger(__name__)


async def act_on_workspace(
    state: TeamPulseState,
    slack_client: Optional[SlackClient] = None,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Execute the Act node logic.

    1. Persist any priority flags into the database with status='pending'.
       Upserts if a pending flag already exists for the same item_id within 24 hours.
    2. Build the Block Kit briefing with Approve and Dismiss buttons.
    3. Post the briefing to Slack via SlackClient.
    4. Store the Slack message timestamp (ts) and channel with the agent run.
    No write actions (Jira comments or tickets) are executed at this point.

    Args:
        state: Current TeamPulseState.
        slack_client: Optional pre-configured SlackClient.
        session: Optional SQLAlchemy database Session.

    Returns:
        State update dictionary containing the briefing details and any errors.
    """
    settings = get_settings()
    workspace_id = state.workspace_id
    logger.info("Executing Act node for workspace: %s", workspace_id)

    db_session = session
    owns_session = False
    if db_session is None:
        session_factory = get_session_factory()
        db_session = session_factory()
        owns_session = True

    flags_with_db: List[Dict[str, Any]] = []
    new_errors: List[str] = list(state.errors)

    try:
        # 1. Save new flags into database with status pending (upsert within 24h window)
        priority_items = state.priority_items or []
        window_start = utc_now() - timedelta(hours=24)

        flags_created_this_run: List[Flag] = []
        all_flags_by_id: Dict[int, Flag] = {}

        for item in priority_items:
            # item may be dict or Pydantic model
            item_id = item.get("item_id", "Unknown") if isinstance(item, dict) else getattr(item, "item_id", "Unknown")
            title = item.get("title", "") if isinstance(item, dict) else getattr(item, "title", "")
            reason = item.get("reason", "") if isinstance(item, dict) else getattr(item, "reason", "")
            urgency = (
                item.get("urgency", URGENCY_WATCH)
                if isinstance(item, dict)
                else getattr(item, "urgency", URGENCY_WATCH)
            ).lower()
            suggested_action = (
                item.get("suggested_action", "")
                if isinstance(item, dict)
                else getattr(item, "suggested_action", "")
            )

            # Extract evidence
            evidence = item.get("evidence") if isinstance(item, dict) else getattr(item, "evidence", None)
            age_days = None
            author = None
            if evidence is not None:
                age_days = evidence.get("age_days") if isinstance(evidence, dict) else getattr(evidence, "age_days", None)
                author = (
                    evidence.get("author_or_assignee")
                    if isinstance(evidence, dict)
                    else getattr(evidence, "author_or_assignee", None)
                )

            item_type = ITEM_TYPE_GITHUB_PR if "#" in item_id else ITEM_TYPE_JIRA_ISSUE
            flag_level = (
                FLAG_LEVEL_ESCALATED
                if urgency in (URGENCY_URGENT, URGENCY_CRITICAL)
                else FLAG_LEVEL_NORMAL
            )

            # Check if pending flag for the same canonical item_id exists within the last 24h
            existing_pending = (
                db_session.query(Flag)
                .filter(
                    Flag.workspace_id == workspace_id,
                    Flag.item_id == item_id,
                    Flag.status == STATUS_PENDING,
                    Flag.created_at >= window_start,
                )
                .order_by(Flag.created_at.desc())
                .first()
            )

            if existing_pending is not None:
                # Upsert: update existing pending flag
                existing_pending.title = title
                existing_pending.flag_reason = reason
                existing_pending.flag_level = flag_level
                existing_pending.urgency = urgency
                existing_pending.suggested_action = suggested_action
                existing_pending.evidence_age_days = age_days
                existing_pending.evidence_author = author
                db_session.flush()
                db_flag = existing_pending
            else:
                db_flag = Flag(
                    workspace_id=workspace_id,
                    item_type=item_type,
                    item_id=item_id,
                    title=title,
                    flag_reason=reason,
                    flag_level=flag_level,
                    urgency=urgency,
                    suggested_action=suggested_action,
                    evidence_age_days=age_days,
                    evidence_author=author,
                    status=STATUS_PENDING,
                    created_at=utc_now(),
                )
                db_session.add(db_flag)
                db_session.flush()
                flags_created_this_run.append(db_flag)

            all_flags_by_id[db_flag.id] = db_flag
            flags_with_db.append({
                "id": db_flag.id,
                "item_id": db_flag.item_id,
                "title": db_flag.title,
                "reason": db_flag.flag_reason,
                "urgency": db_flag.urgency,
                "suggested_action": db_flag.suggested_action,
                "evidence_age_days": db_flag.evidence_age_days,
                "evidence_author": db_flag.evidence_author,
            })

        # 2. Build Slack Block Kit briefing
        briefing_result = build_slack_briefing(state, flags_with_db)
        fallback_text = briefing_result["text"]
        blocks = briefing_result["blocks"]
        included_flag_ids = set(briefing_result.get("included_flag_ids", []))

        slack_channel = None
        slack_ts = None
        post_succeeded = False

        # 3. Post to Slack if configured or client provided
        has_slack_config = bool(settings.slack_bot_token and settings.slack_channel_id)
        if slack_client is not None or has_slack_config:
            client = slack_client or SlackClient()
            target_channel = settings.slack_channel_id
            try:
                slack_res = await client.post_message(
                    channel=target_channel,
                    text=fallback_text,
                    blocks=blocks,
                )
                slack_channel = slack_res.get("channel", target_channel)
                slack_ts = slack_res.get("ts")
                if slack_ts:
                    post_succeeded = True
                    logger.info("Briefing successfully posted to Slack: channel=%s, ts=%s", slack_channel, slack_ts)
                else:
                    logger.error("Slack post_message did not return a valid ts: %s", slack_res)
            except Exception as exc:
                logger.error("Failed to post briefing to Slack: %s", exc)

        if post_succeeded:
            # Write slack_ts and slack_channel on each flag included in the briefing
            for flag_id in included_flag_ids:
                flag_obj = all_flags_by_id.get(flag_id)
                if flag_obj is not None:
                    flag_obj.slack_channel = slack_channel
                    flag_obj.slack_ts = slack_ts

            # Delete any unincluded flags created in this run
            for flag_obj in flags_created_this_run:
                if flag_obj.id not in included_flag_ids:
                    db_session.delete(flag_obj)

            db_session.commit()
        else:
            # Posting failed: delete pending flags created in this run
            logger.info("Slack posting failed or skipped; removing pending flags created in this run.")
            for flag_obj in flags_created_this_run:
                try:
                    db_session.delete(flag_obj)
                except Exception:
                    pass
            db_session.commit()

        # 4. Record agent run in DB with slack_channel and slack_ts
        agent_run = AgentRun(
            workspace_id=workspace_id,
            run_type=state.run_type or "ondemand",
            status="completed",
            query=state.query,
            github_data=json.dumps(state.github_data, default=str) if state.github_data else None,
            jira_data=json.dumps(state.jira_data, default=str) if state.jira_data else None,
            analysis=json.dumps(state.analysis, default=str) if state.analysis else None,
            briefing=json.dumps({"text": fallback_text, "blocks": blocks}, default=str),
            slack_channel=slack_channel,
            slack_ts=slack_ts,
            errors=json.dumps(new_errors) if new_errors else None,
            created_at=utc_now(),
            completed_at=utc_now(),
        )
        db_session.add(agent_run)
        db_session.commit()

        final_briefing: Dict[str, Any] = {
            "text": fallback_text,
            "blocks": blocks,
            "slack_channel": slack_channel,
            "slack_ts": slack_ts,
            "flags_saved": len(included_flag_ids) if post_succeeded else 0,
        }

        return {
            "briefing": final_briefing,
            "errors": new_errors,
        }

    except Exception as exc:
        db_session.rollback()
        logger.error("Error executing act_on_workspace: %s", exc)
        new_errors.append(f"Act node error: {exc}")
        return {
            "briefing": {
                "text": "TeamPulse Briefing failed to generate.",
                "blocks": [],
            },
            "errors": new_errors,
        }
    finally:
        if owns_session:
            db_session.close()
