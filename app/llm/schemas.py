"""Pydantic v2 schemas for LLM structured output and intent routing."""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

UrgencyLevel = Literal["watch", "urgent", "critical"]
ItemSource = Literal["github", "jira", "github_pr", "jira_issue"]


class FlagEvidence(BaseModel):
    """Grounding evidence copied directly from the collected work item."""

    age_days: int = Field(default=0, description="Age in days copied directly from the data")
    author_or_assignee: str = Field(
        default="", description="Author (PR) or assignee (Jira) copied directly from the data"
    )


class PriorityFlag(BaseModel):
    """Represents a flagged issue or pull request requiring team attention."""

    title: str = Field(..., description="Short summary of the flagged item")
    reason: str = Field(..., description="Explanation of why this item is flagged")
    urgency: UrgencyLevel = Field(
        ..., description="Severity level: watch | urgent | critical"
    )
    source: ItemSource = Field(..., description="Source system: github | jira")
    item_id: str = Field(..., description="Unique ID, e.g. canonical ID like '<copy from data>'")
    suggested_action: str = Field(
        ..., description="Recommended action to take on this item"
    )
    evidence: Optional[FlagEvidence] = Field(
        default=None,
        description="Grounding evidence directly copied from the data (age_days, author_or_assignee)",
    )


class Analysis(BaseModel):
    """Operational analysis and pattern detection produced by the Analyze node."""

    patterns: List[str] = Field(
        default_factory=list,
        description="Identified operational patterns, team bottlenecks, or themes",
    )
    priority_flags: List[PriorityFlag] = Field(
        default_factory=list,
        description="List of priority flags requiring action or monitoring",
    )
    shipped_this_period: List[str] = Field(
        default_factory=list,
        description="Items successfully merged or resolved during this period",
    )
    velocity_note: str = Field(
        default="",
        description="Observational note on delivery pace and momentum",
    )
    one_recommendation: str = Field(
        default="",
        description="Single most important recommendation for the team lead today",
    )


class QueryFilters(BaseModel):
    """Filters applicable to work items."""

    source: Optional[str] = Field(
        default=None,
        description="Data source filter: github | jira | None",
    )
    author: Optional[str] = Field(
        default=None,
        description="Filter by author or assignee login",
    )
    status: Optional[str] = Field(
        default=None,
        description="Filter by status (e.g. Open, In Progress, Blocked)",
    )
    stale_only: Optional[bool] = Field(
        default=None,
        description="Whether to only include stale work items",
    )


class QueryIntent(BaseModel):
    """Classification of an on-demand natural language query."""

    sources_needed: List[str] = Field(
        default_factory=list,
        description="Data sources required to answer (e.g. ['github', 'jira'])",
    )
    entities: List[str] = Field(
        default_factory=list,
        description="Identified user mentions, repos, or issue keys",
    )
    query_type: str = Field(
        default="status",
        description="Category of query (e.g. status, blockers, stale, velocity, lookup, out_of_scope)",
    )
    operation: str = Field(
        default="summary",
        description="Operation: rank | count | list | lookup | summary | out_of_scope",
    )
    metric: str = Field(
        default="none",
        description="Metric: age_days | days_since_update | comment_count | none",
    )
    order: str = Field(
        default="desc",
        description="Sort order: asc | desc",
    )
    limit: int = Field(
        default=5,
        description="Limit on results returned (default 5, max 10)",
    )
    filters: QueryFilters = Field(
        default_factory=QueryFilters,
        description="Filters on source, author, status, stale_only",
    )
    entity_ids: List[str] = Field(
        default_factory=list,
        description="Specific canonical work item IDs or keys",
    )
    time_range: str = Field(
        default="24h",
        description="Temporal window relevant to the query (e.g. 24h, 7d, current_sprint)",
    )

    @field_validator("limit", mode="after")
    @classmethod
    def clamp_limit(cls, v: int) -> int:
        if v < 1:
            return 5
        return min(v, 10)




class QueryAnswerOutput(BaseModel):
    """Structured response container for plain-language query answers."""

    answer: str = Field(..., description="Factual answer grounded strictly in collected work items")


class SubTaskSpec(BaseModel):
    """Specific technical subtask within a parent task."""

    title: str = Field(..., description="Short, actionable title for the subtask")
    description: str = Field(default="", description="Technical details, acceptance criteria, or steps")
    estimated_hours: int = Field(default=4, ge=1, description="Estimated work hours (minimum 1)")
    jira_key: Optional[str] = Field(default=None, description="Jira issue key once provisioned (e.g. KAN-12)")


class TaskSpec(BaseModel):
    """User story or technical task under an Epic."""

    title: str = Field(..., description="Short summary of the task")
    description: str = Field(default="", description="Technical requirements, architecture, or deliverables")
    estimated_hours: int = Field(default=8, ge=1, description="Estimated work hours for this task")
    subtasks: List[SubTaskSpec] = Field(default_factory=list, description="List of granular subtasks")
    jira_key: Optional[str] = Field(default=None, description="Jira issue key once provisioned (e.g. KAN-11)")


class EpicSpec(BaseModel):
    """Major project milestone or architectural component."""

    title: str = Field(..., description="Title of the Epic / major milestone")
    description: str = Field(default="", description="High-level scope, objectives, and deliverables")
    estimated_hours: int = Field(default=24, ge=1, description="Estimated hours across all tasks in this epic")
    tasks: List[TaskSpec] = Field(default_factory=list, description="List of technical tasks under this epic")
    jira_key: Optional[str] = Field(default=None, description="Jira issue key once provisioned (e.g. KAN-10)")


class ProjectPlan(BaseModel):
    """Complete project plan broken down into Epics, Tasks, Sub-tasks, and hour allocations."""

    project_name: str = Field(..., description="Inferred or given project name")
    summary: str = Field(..., description="Concise executive summary of the plan and architecture")
    developers_count: int = Field(default=1, ge=1, description="Number of developers assigned")
    weeks_duration: float = Field(default=1.0, ge=0.1, description="Project duration in weeks")
    total_capacity_hours: int = Field(default=40, description="Total available hours (developers * weeks * 40)")
    total_estimated_hours: int = Field(default=0, description="Sum of estimated hours across all tasks/epics")
    epics: List[EpicSpec] = Field(default_factory=list, description="Epics making up the project")
    generated_by: Optional[str] = Field(default=None, description="Engine or model that generated this plan")

