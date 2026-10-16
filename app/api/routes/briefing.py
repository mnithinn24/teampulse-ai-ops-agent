"""Briefing, run status, and natural language query routes."""

import hmac
import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.query import handle_plain_language_query
from app.config import get_settings
from app.core.errors import PipelineConcurrencyError
from app.db.database import get_db_session
from app.db.models import AgentRun
from app.graph.runner import trigger_pipeline_run

logger = logging.getLogger(__name__)

router = APIRouter(tags=["briefing"])


def verify_admin_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> str:
    """Validate that the request includes a valid X-API-Key header matching ADMIN_API_KEY.

    Raises:
        HTTPException 401: If header is missing, wrong, or ADMIN_API_KEY is unset.
    """
    settings = get_settings()
    configured_key = settings.admin_api_key

    if not configured_key:
        logger.warning("Admin API request rejected: ADMIN_API_KEY is not configured.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ADMIN_API_KEY is not configured on the server.",
        )

    if not x_api_key or not hmac.compare_digest(x_api_key.strip(), configured_key.strip()):
        logger.warning("Admin API request rejected: Invalid or missing X-API-Key.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header.",
        )

    return x_api_key


class TriggerBriefingRequest(BaseModel):
    """Payload for manually triggering a briefing run."""

    workspace_id: str = Field(default="default", description="Target workspace ID")
    run_type: str = Field(default="ondemand", description="morning, evening, or ondemand")


class PlainQueryRequest(BaseModel):
    """Payload for asking a plain-language operational question."""

    question: str = Field(..., min_length=1, description="Question text to answer")
    workspace_id: str = Field(default="default", description="Target workspace ID")


@router.post("/briefing/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_briefing(
    body: TriggerBriefingRequest = TriggerBriefingRequest(),
    _: str = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Trigger a new briefing run in the background.

    Requires X-API-Key header.
    Returns HTTP 202 with run_id.
    Returns HTTP 409 if a run is already executing for the workspace.
    """
    try:
        run_id, _ = await trigger_pipeline_run(
            workspace_id=body.workspace_id,
            run_type=body.run_type,
            background=True,
        )
        return {
            "run_id": run_id,
            "status": "running",
            "message": "Briefing run started in background.",
        }
    except PipelineConcurrencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )


@router.get("/runs/{run_id}")
async def get_run_details(run_id: int) -> Dict[str, Any]:
    """Retrieve run execution status, model used, per-node timings, and errors."""
    with get_db_session() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found.",
            )

        parsed_errors: List[str] = []
        if run.errors:
            try:
                parsed_errors = json.loads(run.errors)
            except Exception:
                parsed_errors = [run.errors]

        return {
            "run_id": run.id,
            "workspace_id": run.workspace_id,
            "run_type": run.run_type,
            "status": run.status,
            "model_used": run.model_used or "unknown",
            "fallback_used": bool(run.fallback_used),
            "per_node_timings": {
                "collect_ms": run.collect_latency_ms,
                "analyze_ms": run.analyze_latency_ms,
                "act_ms": run.act_latency_ms,
                "total_ms": run.total_latency_ms,
            },
            "total_latency_ms": run.total_latency_ms,
            "items_collected": run.items_collected,
            "flags_created": run.flags_created,
            "errors": parsed_errors,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }


@router.post("/query")
async def execute_plain_language_query(
    body: PlainQueryRequest,
    _: str = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Answer a natural language question about current pull requests and issues.

    Requires X-API-Key header.
    Returns {answer, sources_used, item_ids, model_used, latency_ms}.
    """
    return await handle_plain_language_query(
        question=body.question.strip(),
        workspace_id=body.workspace_id,
    )
