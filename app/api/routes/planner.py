"""API routes for AI Project Planner."""

import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.planner import (
    generate_project_plan,
    parse_plan_request_text,
    provision_jira_plan,
)
from app.api.routes.briefing import verify_admin_api_key
from app.config import get_settings
from app.integrations.jira_client import JiraClient
from app.llm.schemas import ProjectPlan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/project", tags=["planner"])


class ProjectPlanRequest(BaseModel):
    """Payload for generating and provisioning an AI project plan."""

    description: str = Field(
        ...,
        min_length=3,
        description="Project description (e.g. 'Medical RAG, 2 developers, 2 weeks')",
    )
    developers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional explicit number of developers (extracted from description if omitted)",
    )
    weeks: Optional[float] = Field(
        default=None,
        gt=0,
        description="Optional explicit duration in weeks (extracted from description if omitted)",
    )
    project_key: Optional[str] = Field(
        default=None,
        description="Target Jira project key (e.g. 'KAN'). Defaults to configured JIRA_PROJECT_KEY.",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="Dry-run override. If True, previews tickets without creating them in Jira.",
    )


@router.post("/plan", response_model=ProjectPlan)
async def create_project_plan_endpoint(
    body: ProjectPlanRequest,
    _: str = Depends(verify_admin_api_key),
) -> ProjectPlan:
    """Generate an AI-driven project plan and automatically provision Epics, Tasks, and Subtasks in Jira.

    Requires X-API-Key header.
    Respects DRY_RUN_ACTIONS setting or explicit 'dry_run' parameter.
    """
    settings = get_settings()

    # 1. Parse text parameters if not explicitly provided
    parsed = parse_plan_request_text(body.description)
    devs = body.developers if body.developers is not None else parsed["developers"]
    wks = body.weeks if body.weeks is not None else parsed["weeks"]
    clean_desc = parsed["description"]

    # 2. Generate structured plan with hours and breakdown
    try:
        plan = await generate_project_plan(
            description=clean_desc,
            developers=devs,
            weeks=wks,
        )
    except Exception as exc:
        logger.error("Failed to generate project plan: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Plan generation failed: {exc}",
        ) from exc

    # 3. Provision Epics, Tasks, and Subtasks in Jira
    target_key = body.project_key or settings.jira_project_key or "PROJ"
    try:
        provisioned_plan = await provision_jira_plan(
            plan=plan,
            project_key=target_key,
            dry_run=body.dry_run,
        )
        return provisioned_plan
    except Exception as exc:
        logger.error("Failed to provision Jira tickets for plan: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira provisioning failed: {exc}",
        ) from exc


@router.post("/plan/preview", response_model=ProjectPlan)
async def preview_project_plan_endpoint(
    body: ProjectPlanRequest,
    _: str = Depends(verify_admin_api_key),
) -> ProjectPlan:
    """Preview an AI project plan without calling Jira (forced dry-run).

    Requires X-API-Key header.
    """
    body.dry_run = True
    return await create_project_plan_endpoint(body=body, _=_)


class ProvisionExistingPlanRequest(BaseModel):
    """Payload for provisioning a previously generated plan directly into Jira."""

    plan: ProjectPlan = Field(..., description="ProjectPlan object to provision")
    project_key: Optional[str] = Field(default=None, description="Target Jira project key")
    dry_run: Optional[bool] = Field(default=False, description="Whether to perform dry-run")


@router.post("/provision", response_model=ProjectPlan)
async def provision_existing_plan_endpoint(
    body: ProvisionExistingPlanRequest,
    _: str = Depends(verify_admin_api_key),
) -> ProjectPlan:
    """Provision an existing preview plan into Jira Cloud."""
    settings = get_settings()
    target_key = body.project_key or settings.jira_project_key or "PROJ"
    try:
        provisioned_plan = await provision_jira_plan(
            plan=body.plan,
            project_key=target_key,
            dry_run=body.dry_run,
        )
        return provisioned_plan
    except Exception as exc:
        logger.error("Failed to provision Jira tickets for plan: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira provisioning failed: {exc}",
        ) from exc


@router.get("/config")
async def get_planner_config() -> Dict[str, Any]:
    """Retrieve planner UI configuration, connected Jira project key, and defaults."""
    settings = get_settings()
    return {
        "jira_project_key": settings.jira_project_key or "KAN",
        "jira_base_url": settings.jira_base_url or "",
        "dry_run_default": settings.dry_run_actions,
        "admin_api_key": settings.admin_api_key or "",
    }


class UpdateTicketRequest(BaseModel):
    """Payload for updating an existing task, subtask, or epic in Jira and the plan."""

    jira_key: Optional[str] = Field(
        default=None,
        description="Jira issue key if provisioned (e.g. 'KAN-74', 'DRY-KAN-T1')",
    )
    title: str = Field(..., min_length=1, description="New title / summary")
    description: Optional[str] = Field(default="", description="New description")
    estimated_hours: int = Field(default=1, ge=1, description="Estimated work hours")


@router.post("/ticket/update")
async def update_ticket_endpoint(
    body: UpdateTicketRequest,
    _: str = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Update title, description, and hours of a Jira ticket or preview ticket."""
    if not body.jira_key or body.jira_key.startswith("DRY-"):
        # Local / Dry-run simulated item: no remote Jira call required
        return {
            "status": "success",
            "message": "Local preview ticket updated",
            "jira_key": body.jira_key,
            "title": body.title,
            "description": body.description or "",
            "estimated_hours": body.estimated_hours,
        }

    # Live Jira ticket update
    client = JiraClient()
    try:
        await client.update_issue(
            issue_key=body.jira_key,
            summary=body.title,
            description=body.description or "",
            estimated_hours=body.estimated_hours,
        )
        return {
            "status": "success",
            "message": f"Successfully updated Jira issue {body.jira_key}",
            "jira_key": body.jira_key,
            "title": body.title,
            "description": body.description or "",
            "estimated_hours": body.estimated_hours,
        }
    except Exception as exc:
        logger.error("Failed to update Jira ticket %s: %s", body.jira_key, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to update Jira issue {body.jira_key}: {exc}",
        ) from exc
    finally:
        await client.close()


