"""SQLAlchemy 2.0 ORM models for TeamPulse."""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.constants import (
    FLAG_LEVEL_NORMAL,
    ITEM_TYPE_GITHUB_PR,
    ITEM_TYPE_JIRA_ISSUE,
    STATUS_PENDING,
    URGENCY_WATCH,
)


def utc_now() -> datetime:
    """Return current UTC timestamp without timezone offset for database compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


class Workspace(Base):
    """Represents an integrated engineering team workspace."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    github_repos: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Comma-separated repository list"
    )
    jira_project_key: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class AgentRun(Base):
    """Tracks each pipeline execution run and its state/briefing results."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    run_type: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="morning, evening, or ondemand"
    )
    status: Mapped[str] = mapped_column(
        String(32), default="started", nullable=False, doc="started, completed, failed"
    )
    query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    github_data: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Serialized JSON of fetched GitHub data"
    )
    jira_data: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Serialized JSON of fetched Jira data"
    )
    analysis: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Serialized JSON of LLM analysis"
    )
    briefing: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Generated briefing text or Block Kit JSON"
    )
    slack_channel: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, doc="Slack channel ID where briefing was posted"
    )
    slack_ts: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, doc="Slack message timestamp (ts)"
    )
    errors: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Serialized JSON list of errors encountered"
    )
    total_latency_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Total pipeline latency in ms"
    )
    collect_latency_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Collect node latency in ms"
    )
    analyze_latency_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Analyze node latency in ms"
    )
    act_latency_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Act node latency in ms"
    )
    model_used: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, doc="Model name used for analysis"
    )
    fallback_used: Mapped[Optional[bool]] = mapped_column(
        Boolean, default=False, nullable=True, doc="Whether fallback model or rules were used"
    )
    items_collected: Mapped[Optional[int]] = mapped_column(
        Integer, default=0, nullable=True, doc="Total work items collected"
    )
    flags_created: Mapped[Optional[int]] = mapped_column(
        Integer, default=0, nullable=True, doc="Total priority flags raised in briefing"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )


class Flag(Base):
    """Tracks flagged issues/PRs to avoid duplicate alerting within 24 hours."""

    __tablename__ = "flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    item_type: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="'github_pr' or 'jira_issue'"
    )
    item_id: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False, doc="e.g., 'PROJ-123' or 'owner/repo#42'"
    )
    title: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, doc="Item title or summary"
    )
    flag_reason: Mapped[str] = mapped_column(
        String(255), nullable=False, doc="stale, blocker, unassigned"
    )
    flag_level: Mapped[str] = mapped_column(
        String(32), default=FLAG_LEVEL_NORMAL, nullable=False, doc="normal or escalated"
    )
    urgency: Mapped[Optional[str]] = mapped_column(
        String(32), default=URGENCY_WATCH, nullable=True, doc="critical, urgent, watch"
    )
    suggested_action: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Recommended action description"
    )
    evidence_age_days: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    evidence_author: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=STATUS_PENDING, nullable=False, doc="pending, approved, dismissed, failed"
    )
    approved_by: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, doc="Slack User ID of the decider"
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    result: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Execution outcome, e.g. created Jira key or comment confirmation"
    )
    slack_channel: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, doc="Slack channel ID where the briefing was posted"
    )
    slack_ts: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True, doc="Slack message ts when posted"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, index=True, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class ActionTaken(Base):
    """Audit log of actions suggested, approved, rejected, or executed."""

    __tablename__ = "actions_taken"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(
        String(64), nullable=False, doc="jira_comment, jira_create_ticket, slack_post"
    )
    target_id: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False, doc="Target ticket key or channel"
    )
    payload: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Serialized action parameters or comment content"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="suggested",
        nullable=False,
        doc="suggested, approved, rejected, executed, failed",
    )
    approved_by: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, doc="Slack User ID of the approver"
    )
    result: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Result outcome or created ticket identifier"
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="Error message if execution failed"
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False
    )
