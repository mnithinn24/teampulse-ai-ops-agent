"""AI Project Planner for decomposing software projects into Epics, Tasks, and Subtasks with hour estimation and Jira provisioning."""

from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.db.database import get_session_factory
from app.db.models import ActionTaken, utc_now
from app.integrations.jira_client import JiraClient, validate_jira_project_key
from app.llm.router import LLMRouter
from app.llm.schemas import EpicSpec, ProjectPlan, SubTaskSpec, TaskSpec

logger = logging.getLogger(__name__)

SYSTEM_PLANNER_PROMPT = """You are a Principal Software Architect and Technical Project Lead.
Your task is to analyze a project description and break it down into a comprehensive, production-ready engineering plan.

Requirements:
1. Deconstruct the project into 3 to 5 logical Epics (major architectural components, milestones, or services).
2. Under each Epic, create 2 to 4 concrete Tasks (user stories, feature implementations, API designs).
3. Under each Task, create 2 to 3 granular Subtasks (specific coding, schema design, integration tests, UI wiring).
4. CRITICAL REQUIREMENT FOR TASK DESCRIPTIONS:
   Every Task MUST have a comprehensive, highly actionable 4 to 5 line description so the developer can easily understand and execute the work without ambiguity:
   • Line 1 (Technical Goal): Explain the objective, feature scope, and why this task is needed.
   • Line 2 (Architecture & Schemas): Detail the relevant models, database tables, API contracts, or UI layouts.
   • Line 3 (Implementation Steps): Step-by-step guidance on algorithms, chunking/query logic, third-party libraries, and validations.
   • Line 4 (Dependencies & Edge Cases): Address error handling, timeout retries, input sanitization, and security considerations.
   • Line 5 (Acceptance Criteria): Clear, testable criteria for when the task is considered Done.
5. Every Epic MUST have a 3 to 4 line architectural description explaining milestones, deliverables, and release criteria.
6. Every Subtask MUST have a 2 to 3 line description with specific coding steps and deliverables.
7. Calculate and assign realistic developer work hours for each Subtask.
8. The sum of Subtask hours must equal the Task's estimated hours.
9. The sum of Task hours must equal the Epic's estimated hours.
10. The total estimated hours across all Epics must closely match or equal the total developer capacity:
   Capacity = {developers} developers * {weeks} weeks * 40 hours/week = {total_capacity_hours} hours.
11. Output valid JSON matching the ProjectPlan schema.
"""


def parse_plan_request_text(text: str) -> Dict[str, Any]:
    """Parse natural language project request text to extract description, developer count, and timeline.

    Examples:
    - 'Medical RAG, 2 developers, 2 weeks' -> devs: 2, weeks: 2.0, desc: 'Medical RAG'
    - '3 devs, 1.5 wks: Internal search engine' -> devs: 3, weeks: 1.5, desc: 'Internal search engine'
    - 'Build React chat app, 1 developer, 5 days' -> devs: 1, weeks: 1.0, desc: 'Build React chat app'

    Args:
        text: Free-form input string.

    Returns:
        Dict with keys: 'description', 'developers', 'weeks'.
    """
    cleaned = text.strip()
    developers = 2
    weeks = 2.0

    # 1. Match developers count (e.g. "2 developers", "3 devs", "1 engineer", "4 people")
    dev_match = re.search(
        r"(\d+)\s*(?:devs?|developers?|engineers?|people|persons?|members?)",
        cleaned,
        re.IGNORECASE,
    )
    if dev_match:
        developers = max(1, int(dev_match.group(1)))
        cleaned = cleaned[: dev_match.start()] + " " + cleaned[dev_match.end() :]

    # 2. Match weeks (e.g. "2 weeks", "1.5 wks", "3w")
    week_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:weeks?|wks?|w\b)",
        cleaned,
        re.IGNORECASE,
    )
    if week_match:
        weeks = max(0.5, float(week_match.group(1)))
        cleaned = cleaned[: week_match.start()] + " " + cleaned[week_match.end() :]
    else:
        # Check days (e.g. "10 days", "5 d") -> converted to weeks (5 working days per week)
        day_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:days?|d\b)",
            cleaned,
            re.IGNORECASE,
        )
        if day_match:
            days = float(day_match.group(1))
            weeks = max(0.5, round(days / 5.0, 1))
            cleaned = cleaned[: day_match.start()] + " " + cleaned[day_match.end() :]
        else:
            # Check months (e.g. "1 month", "2 months")
            month_match = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:months?|m\b)",
                cleaned,
                re.IGNORECASE,
            )
            if month_match:
                months = float(month_match.group(1))
                weeks = max(1.0, round(months * 4.0, 1))
                cleaned = cleaned[: month_match.start()] + " " + cleaned[month_match.end() :]

    # Clean up punctuation and whitespace from description
    cleaned_desc = re.sub(r"^[,\s:;-]+|[,\s:;-]+$", "", cleaned).strip()
    cleaned_desc = re.sub(r"\s+", " ", cleaned_desc)

    if not cleaned_desc:
        cleaned_desc = text.strip()

    return {
        "description": cleaned_desc,
        "developers": developers,
        "weeks": weeks,
    }


def normalize_and_balance_hours(
    plan: ProjectPlan,
    developers: int,
    weeks: float,
) -> ProjectPlan:
    """Ensure mathematical consistency across subtasks, tasks, epics, and developer capacity.

    Args:
        plan: Initial parsed or generated ProjectPlan.
        developers: Number of developers.
        weeks: Duration in weeks.

    Returns:
        Balanced ProjectPlan with synchronized hour estimates.
    """
    total_capacity = int(round(developers * weeks * 40))
    plan.developers_count = developers
    plan.weeks_duration = weeks
    plan.total_capacity_hours = total_capacity

    # 1. Ensure tasks and subtasks exist for every epic and calculate task hours
    for epic in plan.epics:
        if not epic.tasks:
            # Failsafe: synthesize 2 tasks under the epic if LLM returned 0 tasks
            epic_hrs = max(20, total_capacity // max(1, len(plan.epics)))
            h1 = epic_hrs // 2
            h2 = epic_hrs - h1
            clean_title = epic.title.replace("[Epic]", "").strip()
            epic.tasks = [
                TaskSpec(
                    title=f"{clean_title} - Core Architecture & Pipeline",
                    description=(
                        f"• Technical Goal: Implement core engineering components, schemas, and pipeline for {clean_title}.\n"
                        f"• Architecture: Design models, schemas, and storage abstractions.\n"
                        f"• Implementation Steps: Write module interfaces, data structures, and execution logic.\n"
                        f"• Error Handling: Validate inputs, handle exception retries, and ensure fault tolerance.\n"
                        f"• Acceptance Criteria: Core functionality operational with unit test coverage."
                    ),
                    estimated_hours=h1,
                    subtasks=[
                        SubTaskSpec(
                            title=f"{clean_title} - Core Implementation",
                            description=f"Initial code implementation and data structures for {clean_title}.",
                            estimated_hours=max(1, h1 // 2),
                        ),
                        SubTaskSpec(
                            title=f"{clean_title} - Integration Wiring",
                            description="Integration wiring, error handling, and parameter validation.",
                            estimated_hours=max(1, h1 - (h1 // 2)),
                        ),
                    ],
                ),
                TaskSpec(
                    title=f"{clean_title} - Services, APIs & Verification",
                    description=(
                        f"• Technical Goal: Expose endpoints, business services, and testing harnesses for {clean_title}.\n"
                        f"• Architecture: Connect service layers with controllers, routes, and client callers.\n"
                        f"• Implementation Steps: Implement REST endpoints, business validation, and data formatting.\n"
                        f"• Error Handling: Handle network timeouts, boundary edge cases, and logging.\n"
                        f"• Acceptance Criteria: End-to-end integration passes automated test suite."
                    ),
                    estimated_hours=h2,
                    subtasks=[
                        SubTaskSpec(
                            title=f"{clean_title} - API Endpoint Implementation",
                            description="Build API endpoints and integrate service workflows.",
                            estimated_hours=max(1, h2 // 2),
                        ),
                        SubTaskSpec(
                            title=f"{clean_title} - Automated Tests & Verification",
                            description="Unit and integration test cases covering edge cases.",
                            estimated_hours=max(1, h2 - (h2 // 2)),
                        ),
                    ],
                ),
            ]

        for task in epic.tasks:
            if not task.subtasks:
                half = max(1, task.estimated_hours // 2)
                task.subtasks = [
                    SubTaskSpec(
                        title=f"{task.title} - Core Implementation",
                        description=f"Initial code implementation for {task.title}",
                        estimated_hours=half,
                    ),
                    SubTaskSpec(
                        title=f"{task.title} - Testing & Verification",
                        description=f"Unit tests, error handling, and validation for {task.title}",
                        estimated_hours=max(1, task.estimated_hours - half),
                    ),
                ]
            # Synchronize task hours to sum of subtasks
            task.estimated_hours = sum(st.estimated_hours for st in task.subtasks)

        # Synchronize epic hours to sum of tasks
        epic.estimated_hours = sum(t.estimated_hours for t in epic.tasks)

    # Calculate plan total estimated hours
    raw_total = sum(e.estimated_hours for e in plan.epics)
    if raw_total <= 0:
        raw_total = total_capacity

    # 2. Scale hours proportionally if LLM total deviates from total capacity by > 20%
    ratio = total_capacity / raw_total
    if ratio < 0.7 or ratio > 1.3:
        for epic in plan.epics:
            for task in epic.tasks:
                for subtask in task.subtasks:
                    subtask.estimated_hours = max(1, int(round(subtask.estimated_hours * ratio)))
                task.estimated_hours = sum(st.estimated_hours for st in task.subtasks)
            epic.estimated_hours = sum(t.estimated_hours for t in epic.tasks)

    plan.total_estimated_hours = sum(e.estimated_hours for e in plan.epics)
    return plan


def rule_based_fallback_plan(
    description: str,
    developers: int,
    weeks: float,
) -> ProjectPlan:
    """Deterministic fallback project planner when LLMs are unavailable or offline.

    Generates a realistic 4-Epic breakdown (Architecture, Backend, Frontend, QA)
    tailored to the prompt with exact hour balancing.

    Args:
        description: Project requirements text.
        developers: Developer count.
        weeks: Duration in weeks.

    Returns:
        Structured ProjectPlan instance.
    """
    total_capacity = int(round(developers * weeks * 40))

    # Derive short project title
    first_phrase = description.split(",")[0].split(".")[0].strip()
    project_name = first_phrase if len(first_phrase) <= 50 else first_phrase[:47] + "..."
    if not project_name:
        project_name = "Engineering Project Plan"

    # Allocations across 4 core phases:
    # 1. Core Architecture & Pipeline: ~30%
    # 2. Backend & API Services: ~30%
    # 3. Frontend UI & Integration: ~25%
    # 4. Testing, Evaluation & QA: ~15%
    h_arch = int(round(total_capacity * 0.30))
    h_backend = int(round(total_capacity * 0.30))
    h_frontend = int(round(total_capacity * 0.25))
    h_qa = total_capacity - (h_arch + h_backend + h_frontend)

    epics = [
        EpicSpec(
            title=f"Epic 1: System Architecture & Data Pipeline",
            description=(
                f"Architectural foundation and persistent data ingestion pipeline for {project_name}.\n"
                "Decomposes system requirements into relational database models, vector index collections, and caching layers.\n"
                "Ensures zero data loss during document uploads, idempotent schema migrations, and fast embedding similarity search.\n"
                "Key deliverables include SQLAlchemy models, Alembic migrations, vector search indices, and caching resilience."
            ),
            estimated_hours=h_arch,
            tasks=[
                TaskSpec(
                    title="Design data schemas and storage layer",
                    description=(
                        f"• Technical Goal: Architect persistent relational schemas, vector collection indices, and database models for {project_name}.\n"
                        "• Data Architecture: Define SQLAlchemy models for users, documents, text chunks, and embedding metadata with foreign key constraints.\n"
                        "• Storage Engine: Set up vector collection embeddings with cosine similarity distance metric and indexed metadata filters.\n"
                        "• Implementation Steps: Write idempotent Alembic migration scripts, connection pooling with retry handlers, and transaction rollback logic.\n"
                        "• Acceptance Criteria: All schema migrations execute cleanly; relational tables and vector indices pass automated health check suites."
                    ),
                    estimated_hours=h_arch // 2,
                    subtasks=[
                        SubTaskSpec(
                            title="Define database models & migration scripts",
                            description=(
                                "Write SQLAlchemy models for relational tables and metadata indices with foreign key relationships.\n"
                                "Implement Alembic migration scripts, connection pooling, and automated rollback test fixtures."
                            ),
                            estimated_hours=(h_arch // 2) // 2,
                        ),
                        SubTaskSpec(
                            title="Implement data validation & ingestion parser",
                            description=(
                                "Build document parser supporting PDF, TXT, and Markdown with input sanitization and character stripping.\n"
                                "Implement recursive text chunking strategy (500 tokens, 50-token overlap) with chunk hash validation."
                            ),
                            estimated_hours=(h_arch // 2) - ((h_arch // 2) // 2),
                        ),
                    ],
                ),
                TaskSpec(
                    title="Implement retrieval & processing core",
                    description=(
                        "• Technical Goal: Build the document ingestion pipeline, text extraction algorithms, and semantic vector retrieval engine.\n"
                        "• Data Processing: Parse structured/unstructured documents, stripping control characters and generating deterministic chunk hashes.\n"
                        "• Vectorization: Integrate embedding generation pipeline with batch processing (32 chunks/batch) and dimensional checks.\n"
                        "• Query Execution: Implement hybrid top-k vector retrieval with similarity score thresholding and re-ranking algorithms.\n"
                        "• Acceptance Criteria: End-to-end ingestion indexes a 100-page document within 3 seconds; search queries return ranked, cited chunks."
                    ),
                    estimated_hours=h_arch - (h_arch // 2),
                    subtasks=[
                        SubTaskSpec(
                            title="Build vector search / query execution engine",
                            description=(
                                "Integrate vector database client with top-k nearest neighbor search and score threshold filtering.\n"
                                "Implement query embedding pipeline with vector normalization and cited chunk extraction."
                            ),
                            estimated_hours=(h_arch - (h_arch // 2)) // 2,
                        ),
                        SubTaskSpec(
                            title="Add caching and retry resilience",
                            description=(
                                "Set up in-memory cache for repeated query embeddings with configurable TTL expiration.\n"
                                "Add backoff retry handlers with exponential jitter for upstream model API network failures."
                            ),
                            estimated_hours=(h_arch - (h_arch // 2)) - ((h_arch - (h_arch // 2)) // 2),
                        ),
                    ],
                ),
            ],
        ),
        EpicSpec(
            title=f"Epic 2: API Services & Business Logic",
            description=(
                f"RESTful FastAPI endpoints, authentication middleware, and core application workflows for {project_name}.\n"
                "Provides secure communication channels between frontend clients, data layers, and downstream models.\n"
                "Enforces strict schema validation, rate limiting, and telemetry audit logging across all operations.\n"
                "Key deliverables include verified API routes, token security filters, and audit trail storage."
            ),
            estimated_hours=h_backend,
            tasks=[
                TaskSpec(
                    title="Develop core API routes and request validation",
                    description=(
                        "• Technical Goal: Build high-performance RESTful API endpoints for file ingestion, query processing, and system status.\n"
                        "• API Endpoints: Implement POST /upload (multipart file handling), POST /query (natural language Q&A), and GET /status.\n"
                        "• Request Verification: Use Pydantic v2 schemas for strict payload verification, sanitizing user inputs and enforcing size limits.\n"
                        "• Async Architecture: Utilize background worker tasks for asynchronous document indexing to keep endpoints non-blocking.\n"
                        "• Acceptance Criteria: Endpoints return appropriate HTTP status codes (200, 400, 422, 500) with structured JSON error details."
                    ),
                    estimated_hours=h_backend // 2,
                    subtasks=[
                        SubTaskSpec(
                            title="Implement POST /upload and ingestion endpoints",
                            description=(
                                "Build multipart file upload handler supporting PDF/EHR documents with size and MIME validation.\n"
                                "Dispatch async processing jobs to background tasks and return immediate task tracker ID."
                            ),
                            estimated_hours=(h_backend // 2) // 2,
                        ),
                        SubTaskSpec(
                            title="Implement query answering and streaming response routes",
                            description=(
                                "Create question-answering endpoints with cited chunk references and token usage telemetry.\n"
                                "Implement streaming response routes (SSE/chunked HTTP) with structured fallback error handling."
                            ),
                            estimated_hours=(h_backend // 2) - ((h_backend // 2) // 2),
                        ),
                    ],
                ),
                TaskSpec(
                    title="Security, rate limiting & audit logging",
                    description=(
                        "• Technical Goal: Protect application endpoints against unauthorized access, abusive traffic bursts, and maintain complete audit observability.\n"
                        "• Authentication: Implement JWT token verification middleware with role-based access control (RBAC) and API key guards.\n"
                        "• Rate Limiting: Set up sliding-window rate limiters per client IP/token to throttle excessive requests with HTTP 429 headers.\n"
                        "• Telemetry: Add structured JSON logging with correlation IDs, latency tracking, and sanitization of sensitive patient/user data.\n"
                        "• Acceptance Criteria: Unauthenticated requests return 401; rate limiter blocks bursts exceeding 60 req/min with Retry-After header."
                    ),
                    estimated_hours=h_backend - (h_backend // 2),
                    subtasks=[
                        SubTaskSpec(
                            title="Integrate token auth & rate limiting middleware",
                            description=(
                                "Implement sliding window rate limiting middleware to prevent API abuse with HTTP 429 headers.\n"
                                "Add JWT token verification and API key inspection headers for protected endpoints."
                            ),
                            estimated_hours=(h_backend - (h_backend // 2)) // 2,
                        ),
                        SubTaskSpec(
                            title="Add execution audit logging & telemetry",
                            description=(
                                "Store execution metrics, query latencies, and system health checks in structured JSON logs.\n"
                                "Implement data masking to strip PII and sensitive tokens before writing to audit logs."
                            ),
                            estimated_hours=(h_backend - (h_backend // 2)) - ((h_backend - (h_backend // 2)) // 2),
                        ),
                    ],
                ),
            ],
        ),
        EpicSpec(
            title=f"Epic 3: User Interface & Frontend Experience",
            description=(
                f"Interactive web UI, file upload widgets, question input, and real-time display for {project_name}.\n"
                "Delivers an intuitive, accessible user experience with responsive desktop and mobile support.\n"
                "Features live file upload indicators, streaming conversational streams, and interactive citation highlights.\n"
                "Key deliverables include reusable components, global state store, and resilient API integration layers."
            ),
            estimated_hours=h_frontend,
            tasks=[
                TaskSpec(
                    title="Build UI components and state management",
                    description=(
                        "• Technical Goal: Create an intuitive, modern responsive user interface for document management and interactive query answering.\n"
                        "• Component Design: Build drag-and-drop file upload dropzone with upload progress bars, file validation, and error alerts.\n"
                        "• State Management: Implement lightweight state store managing active documents, conversation history, and loading indicators.\n"
                        "• Interaction Flow: Design chat stream interface displaying cited document excerpts, timestamps, and confidence scores.\n"
                        "• Acceptance Criteria: File upload handles drag-and-drop with size checks; chat interface renders responses with highlighted citations."
                    ),
                    estimated_hours=h_frontend // 2,
                    subtasks=[
                        SubTaskSpec(
                            title="Implement file upload dropzone and progress indicator",
                            description=(
                                "Build drag-and-drop file upload component with real-time percentage progress indicators.\n"
                                "Add MIME-type validation, error toasts for oversized files, and cancel upload button."
                            ),
                            estimated_hours=(h_frontend // 2) // 2,
                        ),
                        SubTaskSpec(
                            title="Create question-and-answer chat interface",
                            description=(
                                "Develop conversational stream interface with auto-scrolling and timestamped messages.\n"
                                "Format cited reference cards that expand when clicked to reveal grounded document excerpts."
                            ),
                            estimated_hours=(h_frontend // 2) - ((h_frontend // 2) // 2),
                        ),
                    ],
                ),
                TaskSpec(
                    title="API client integration and error boundary handling",
                    description=(
                        "• Technical Goal: Wire frontend components to backend REST endpoints with resilient network handling and graceful error recovery.\n"
                        "• HTTP Client: Implement centralized API client with timeout handling, exponential backoff retries, and auth token injection.\n"
                        "• Error Handling: Implement React error boundaries and toast notifications for connection drops, validation errors, and 5xx responses.\n"
                        "• UX Polish: Add optimistic UI updates, skeleton loading placeholders, and clean keyboard navigation shortcuts.\n"
                        "• Acceptance Criteria: UI recovers cleanly from backend disconnects; user receives descriptive toast alerts without page crashes."
                    ),
                    estimated_hours=h_frontend - (h_frontend // 2),
                    subtasks=[
                        SubTaskSpec(
                            title="Connect frontend API service to backend routes",
                            description=(
                                "Implement centralized HTTP service with request timeout handlers and bearer token injection.\n"
                                "Wire upload and query submission events to backend endpoints with loading indicators."
                            ),
                            estimated_hours=(h_frontend - (h_frontend // 2)) // 2,
                        ),
                        SubTaskSpec(
                            title="Add responsive styling, dark mode & error boundaries",
                            description=(
                                "Implement CSS media queries for full mobile/tablet/desktop responsiveness and dark theme palette.\n"
                                "Wrap key components in error boundaries with retry buttons to catch unhandled runtime errors."
                            ),
                            estimated_hours=(h_frontend - (h_frontend // 2)) - ((h_frontend - (h_frontend // 2)) // 2),
                        ),
                    ],
                ),
            ],
        ),
        EpicSpec(
            title=f"Epic 4: Quality Assurance, Evaluation & Deployment",
            description=(
                f"Comprehensive automated test suite, offline evaluation harness, documentation, and release pipeline for {project_name}.\n"
                "Guarantees regression-free software delivery, zero hallucination retrieval, and complete operational readiness.\n"
                "Validates all unit, integration, and end-to-end flows with deterministic simulated fixtures.\n"
                "Key deliverables include automated test harnesses, setup scripts, operational runbooks, and demo sign-offs."
            ),
            estimated_hours=h_qa,
            tasks=[
                TaskSpec(
                    title="Automated testing and benchmark evaluation suite",
                    description=(
                        "• Technical Goal: Establish comprehensive automated test coverage across backend business logic, API routes, and retrieval accuracy.\n"
                        "• Unit & Integration: Write pytest test cases exercising document parsers, database migrations, and edge-case inputs.\n"
                        "• Accuracy Benchmark: Build offline evaluation harness verifying answer precision, citation accuracy, and hallucination rate.\n"
                        "• Mock Isolation: Implement mock HTTP transports and in-memory SQLite fixtures to allow 100% offline, deterministic CI test runs.\n"
                        "• Acceptance Criteria: Test suite passes with >= 85% code coverage; benchmark harness achieves 100% test pass rate with 0 network calls."
                    ),
                    estimated_hours=h_qa // 2,
                    subtasks=[
                        SubTaskSpec(
                            title="Write unit & integration test suites",
                            description=(
                                "Write comprehensive pytest test cases covering endpoint routes, error validations, and edge cases.\n"
                                "Use in-memory database fixtures and mock HTTP transports to ensure tests run fast and offline."
                            ),
                            estimated_hours=(h_qa // 2) // 2,
                        ),
                        SubTaskSpec(
                            title="Build accuracy evaluation benchmark",
                            description=(
                                "Build evaluation harness comparing generated answers against reference ground truth datasets.\n"
                                "Measure precision, citation accuracy, and verify zero hallucination rate across test runs."
                            ),
                            estimated_hours=(h_qa // 2) - ((h_qa // 2) // 2),
                        ),
                    ],
                ),
                TaskSpec(
                    title="Deployment automation and documentation",
                    description=(
                        "• Technical Goal: Provide complete operational tooling, developer documentation, and one-click local startup automation.\n"
                        "• Developer Guide: Write detailed README instructions covering environment configuration, prerequisites, and API usage examples.\n"
                        "• Automation Scripts: Provide cross-platform run scripts for automated database setup, migrations, and service startup.\n"
                        "• Production Readiness: Configure health check endpoints, environment variable validators, and graceful shutdown handlers.\n"
                        "• Acceptance Criteria: New team members can clone the repo and run the full stack successfully within 5 minutes following the guide."
                    ),
                    estimated_hours=h_qa - (h_qa // 2),
                    subtasks=[
                        SubTaskSpec(
                            title="Create setup scripts and environment documentation",
                            description=(
                                "Write step-by-step developer installation guide covering Python virtualenv, .env config, and ports.\n"
                                "Create cross-platform PowerShell and Bash start scripts to verify environment health on boot."
                            ),
                            estimated_hours=(h_qa - (h_qa // 2)) // 2,
                        ),
                        SubTaskSpec(
                            title="Final system verification and stakeholder demo",
                            description=(
                                "Perform full end-to-end user verification testing the complete project workflow.\n"
                                "Record feature demo, document operational runbook, and verify all acceptance criteria pass."
                            ),
                            estimated_hours=(h_qa - (h_qa // 2)) - ((h_qa - (h_qa // 2)) // 2),
                        ),
                    ],
                ),
            ],
        ),
    ]

    plan = ProjectPlan(
        project_name=project_name,
        summary=(
            f"Production engineering plan for {project_name}. "
            f"Structured into 4 Epics, 8 Tasks, and 16 Subtasks matching "
            f"{total_capacity} hours across {developers} developer(s) over {weeks} week(s)."
        ),
        developers_count=developers,
        weeks_duration=weeks,
        total_capacity_hours=total_capacity,
        total_estimated_hours=total_capacity,
        epics=epics,
        generated_by="rule_based_fallback",
    )
    return normalize_and_balance_hours(plan, developers, weeks)


async def generate_project_plan(
    description: str,
    developers: int = 2,
    weeks: float = 2.0,
    router: Optional[LLMRouter] = None,
) -> ProjectPlan:
    """Generate a structured project plan with Epics, Tasks, Subtasks, and hours using LLM with fallback.

    Args:
        description: Project description and technical goals.
        developers: Number of developers assigned.
        weeks: Duration in weeks.
        router: Optional pre-configured LLMRouter instance.

    Returns:
        Validated and hour-balanced ProjectPlan instance.
    """
    total_capacity = int(round(developers * weeks * 40))
    llm_router = router or LLMRouter()

    system_prompt = SYSTEM_PLANNER_PROMPT.format(
        developers=developers,
        weeks=weeks,
        total_capacity_hours=total_capacity,
    )
    user_prompt = (
        f"Project Description: {description.strip()}\n"
        f"Assigned Developers: {developers}\n"
        f"Timeline: {weeks} weeks ({total_capacity} hours total)\n\n"
        "Create a comprehensive Jira project plan with Epics, Tasks, Subtasks, and realistic hours."
    )

    try:
        result = await llm_router.route(
            prompt=user_prompt,
            schema=ProjectPlan,
            task_type="analysis",
            system_prompt=system_prompt,
        )
        plan = getattr(result, "data", getattr(result, "parsed", None))
        if plan and plan.epics:
            model_info = getattr(result, "model_name", "llm")
            provider_info = getattr(result, "provider", "")
            engine_label = f"{provider_info}: {model_info}" if provider_info else model_info
            logger.info(
                "Successfully generated project plan via LLM (%s): %s (%d epics)",
                engine_label,
                plan.project_name,
                len(plan.epics),
            )
            plan.generated_by = engine_label
            return normalize_and_balance_hours(plan, developers, weeks)
    except Exception as exc:
        logger.warning(
            "LLM project plan generation failed (%s). Falling back to rule-based planner.",
            str(exc)[:150],
        )

    logger.info("Using deterministic rule-based planner for %s", description[:40])
    fallback_plan = rule_based_fallback_plan(description, developers, weeks)
    fallback_plan.generated_by = "rule_based_fallback"
    return fallback_plan


async def provision_jira_plan(
    plan: ProjectPlan,
    project_key: Optional[str] = None,
    jira_client: Optional[JiraClient] = None,
    dry_run: Optional[bool] = None,
    session_factory: Optional[Any] = None,
) -> ProjectPlan:
    """Create Epics, Tasks, and Subtasks in Jira matching the plan hierarchy.

    Respects dry_run flag: if True, returns simulated keys without making Jira API calls.

    Args:
        plan: Populated ProjectPlan model.
        project_key: Jira project key (e.g. 'KAN'). Defaults to settings.jira_project_key.
        jira_client: Optional pre-configured JiraClient.
        dry_run: Optional dry_run override. Defaults to settings.dry_run_actions.
        session_factory: Optional database session factory for audit logging.

    Returns:
        Updated ProjectPlan with populated Jira keys.
    """
    settings = get_settings()
    is_dry_run = dry_run if dry_run is not None else settings.dry_run_actions

    raw_key = project_key or settings.jira_project_key or "PROJ"
    target_key = validate_jira_project_key(raw_key)

    logger.info(
        "Provisioning project plan '%s' in Jira project %s (dry_run=%s)",
        plan.project_name,
        target_key,
        is_dry_run,
    )

    client: Optional[JiraClient] = None
    if not is_dry_run:
        client = jira_client or JiraClient(project_key=target_key)

    epic_counter = 1
    task_counter = 1
    subtask_counter = 1

    try:
        for epic in plan.epics:
            epic_desc = f"{epic.description}\n\nEstimated Hours: {epic.estimated_hours}h"
            if is_dry_run:
                epic.jira_key = f"DRY-{target_key}-E{epic_counter}"
                epic_counter += 1
            else:
                assert client is not None
                try:
                    epic_issue = await client.create_issue(
                        project_key=target_key,
                        summary=f"[Epic] {epic.title}",
                        description=epic_desc,
                        issue_type="Epic",
                    )
                except Exception as exc:
                    logger.warning("Creating issue with type 'Epic' failed (%s); falling back to 'Task'", exc)
                    epic_issue = await client.create_issue(
                        project_key=target_key,
                        summary=f"[Epic] {epic.title}",
                        description=epic_desc,
                        issue_type="Task",
                    )
                epic.jira_key = epic_issue.key

            # Create Tasks under Epic
            for task in epic.tasks:
                task_desc = (
                    f"{task.description}\n\n"
                    f"Parent Epic: {epic.title} ({epic.jira_key})\n"
                    f"Estimated Hours: {task.estimated_hours}h"
                )
                if is_dry_run:
                    task.jira_key = f"DRY-{target_key}-T{task_counter}"
                    task_counter += 1
                else:
                    assert client is not None
                    task_issue = await client.create_issue(
                        project_key=target_key,
                        summary=task.title,
                        description=task_desc,
                        issue_type="Task",
                        parent_key=epic.jira_key,
                    )
                    task.jira_key = task_issue.key

                # Create Subtasks under Task
                for subtask in task.subtasks:
                    subtask_desc = (
                        f"{subtask.description}\n\n"
                        f"Parent Task: {task.title} ({task.jira_key})\n"
                        f"Estimated Hours: {subtask.estimated_hours}h"
                    )
                    if is_dry_run:
                        subtask.jira_key = f"DRY-{target_key}-S{subtask_counter}"
                        subtask_counter += 1
                    else:
                        assert client is not None
                        subtask_issue = await client.create_issue(
                            project_key=target_key,
                            summary=subtask.title,
                            description=subtask_desc,
                            issue_type="Subtask",
                            parent_key=task.jira_key,
                        )
                        subtask.jira_key = subtask_issue.key

        # Audit log the action
        total_epics = len(plan.epics)
        total_tasks = sum(len(e.tasks) for e in plan.epics)
        total_subtasks = sum(sum(len(t.subtasks) for t in e.tasks) for e in plan.epics)
        status_str = "dry_run" if is_dry_run else "executed"
        result_str = (
            f"Plan '{plan.project_name}': {total_epics} Epics, "
            f"{total_tasks} Tasks, {total_subtasks} Subtasks provisioned in {target_key} "
            f"({plan.total_estimated_hours}h / {plan.total_capacity_hours}h capacity)"
        )

        try:
            factory = session_factory or get_session_factory()
            with factory() as session:
                action_record = ActionTaken(
                    workspace_id="default",
                    action_type="jira_project_plan",
                    target_id=f"{target_key}:{plan.project_name[:50]}",
                    payload=plan.model_dump_json(),
                    status=status_str,
                    approved_by="api_user",
                    result=result_str,
                    executed_at=utc_now(),
                    created_at=utc_now(),
                )
                session.add(action_record)
                session.commit()
        except Exception as audit_err:
            logger.warning("Could not record ActionTaken audit entry: %s", audit_err)

        return plan

    finally:
        if client is not None and jira_client is None:
            await client.close()


def format_plan_for_slack(plan: ProjectPlan, dry_run: bool = False) -> str:
    """Format project plan hierarchy into clean Slack mrkdwn text.

    Args:
        plan: ProjectPlan model instance.
        dry_run: Whether issues were provisioned in dry-run mode.

    Returns:
        Formatted mrkdwn string.
    """
    mode_tag = " _[DRY RUN - No Jira issues created]_" if dry_run else ""
    lines = [
        f":dart: *AI Project Plan: {plan.project_name}*{mode_tag}",
        f"> *Summary:* {plan.summary}",
        f"> *Resources:* {plan.developers_count} developer(s) | *Duration:* {plan.weeks_duration} week(s) | *Capacity:* {plan.total_capacity_hours}h | *Estimated:* {plan.total_estimated_hours}h",
        "",
        "*Project Work Breakdown & Jira Hierarchy:*",
    ]

    for epic in plan.epics:
        epic_key = f"`{epic.jira_key}` " if epic.jira_key else ""
        lines.append(f"• :package: *Epic:* {epic_key}*{epic.title}* ({epic.estimated_hours}h)")
        for task in epic.tasks:
            task_key = f"`{task.jira_key}` " if task.jira_key else ""
            lines.append(f"    └─ :memo: *Task:* {task_key}{task.title} ({task.estimated_hours}h)")
            for subtask in task.subtasks:
                st_key = f"`{subtask.jira_key}` " if subtask.jira_key else ""
                lines.append(f"        └─ :hammer_and_wrench: {st_key}{subtask.title} ({subtask.estimated_hours}h)")

    return "\n".join(lines)
