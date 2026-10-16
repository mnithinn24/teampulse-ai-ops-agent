"""Shared pipeline state representation for TeamPulse."""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field

from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue

RunType = Literal["morning", "evening", "ondemand"]


class TeamPulseState(BaseModel):
    """Shared state model passed between nodes in the LangGraph ops pipeline.

    Attributes:
        workspace_id: Unique identifier for the team workspace.
        run_type: Type of pipeline invocation (morning, evening, or ondemand).
        query: Optional on-demand focus query or filter.
        github_data: Collected GitHub PRs or unavailability status.
        jira_data: Collected Jira issues or unavailability status.
        analysis: Analysis and pattern insights produced by the Analyze node.
        priority_items: Extracted high-priority items requiring attention.
        briefing: Formatted briefing payload or Slack Block Kit data.
        previous_flags: List of recently flagged items loaded from DB.
        errors: Log of non-fatal operational errors encountered during execution.
    """

    workspace_id: str = Field(default="default", description="Workspace ID")
    run_type: str = Field(default="morning", description="morning | evening | ondemand")
    query: Optional[str] = Field(default=None, description="Optional focus query")
    run_id: Optional[int] = Field(default=None, description="Optional DB AgentRun ID")

    github_data: Union[List[PullRequest], Dict[str, Any], str] = Field(
        default_factory=list,
        description="Collected GitHub pull requests or status dict",
    )
    jira_data: Union[List[JiraIssue], Dict[str, Any], str] = Field(
        default_factory=list,
        description="Collected Jira tickets or status dict",
    )

    analysis: Optional[Union[Dict[str, Any], str]] = Field(
        default=None,
        description="Analysis summary or structured insights",
    )
    priority_items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Filtered high-priority items",
    )
    briefing: Optional[Union[Dict[str, Any], str]] = Field(
        default=None,
        description="Generated Slack briefing blocks or markdown",
    )
    previous_flags: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Items flagged within the past 24 hours",
    )
    errors: List[str] = Field(
        default_factory=list,
        description="Errors encountered across stages",
    )

    def __getitem__(self, item: str) -> Any:
        """Allow dict-like indexing access for compatibility."""
        return getattr(self, item)

    def __setitem__(self, key: str, value: Any) -> None:
        """Allow dict-like assignment for compatibility."""
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        """Allow 'in' membership check for compatibility."""
        return hasattr(self, key)
