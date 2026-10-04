"""Stage 5 governed AI, agent, controls, and usage API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..access_control.engine import ResourceContext, authorization_engine
from ..access_control.service import primary_organization
from ..ai import agent_runtime, ledger_analytics, signal_engine
from ..ai.gateway import AIProviderError
from ..ai.personal_activity import personal_activity
from ..ai.service import ai_service
from ..security import Principal, current_principal


router = APIRouter(prefix="/api/v1/ai-platform", tags=["ai-platform"])


class ChatRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=80)
    feature: str = Field(default="ENTERPRISE_COPILOT", max_length=80)
    resource_type: str = Field(default="ORGANIZATION", max_length=60)
    resource_id: str | None = Field(default=None, max_length=160)
    repository_id: str | None = Field(default=None, max_length=100)
    node_id: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=240)


class ControlScanRequest(BaseModel):
    repository_id: str | None = Field(default=None, max_length=100)


class SignalConditionModel(BaseModel):
    field: str = Field(min_length=1, max_length=60)
    op: str = Field(min_length=1, max_length=30)
    value: Any = None


class CreateSignalRequest(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=40)
    insight_type: str | None = Field(default=None, max_length=60)
    source: str = Field(min_length=2, max_length=60)
    condition: SignalConditionModel
    severity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")


class UpdateSignalRequest(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    condition: SignalConditionModel | None = None
    severity: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    enabled: bool | None = None


class SignalScanRequest(BaseModel):
    repository_id: str | None = Field(default=None, max_length=100)
    signal_id: str | None = Field(default=None, max_length=100)


class RecommendationRequest(BaseModel):
    title: str = Field(min_length=2, max_length=240)
    recommendation: str = Field(min_length=3, max_length=4000)
    priority: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    ai_request_id: str | None = Field(default=None, max_length=100)


class RecommendationDecision(BaseModel):
    decision: str = Field(pattern="^(ACCEPTED|REJECTED)$")
    reason: str = Field(min_length=2, max_length=1000)


class AgentRunRequest(BaseModel):
    agent_key: str = Field(min_length=3, max_length=100)
    goal: str = Field(min_length=3, max_length=4000)
    resource_type: str = Field(default="ORGANIZATION", max_length=60)
    resource_id: str | None = Field(default=None, max_length=160)
    repository_id: str | None = Field(default=None, max_length=100)
    node_id: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=240)
    requested_action: dict[str, Any] | None = None


class OrganizationAISettings(BaseModel):
    ai_enabled: bool = True
    external_ai_enabled: bool = True
    allowed_classifications: list[str] = Field(default_factory=lambda: ["PUBLIC", "INTERNAL", "CONFIDENTIAL"], max_length=10)
    daily_token_quota: int = Field(default=1000000, ge=1000, le=1000000000)
    user_daily_token_quota: int = Field(default=150000, ge=1000, le=100000000)
    agent_actions_enabled: bool = True
    retention_days: int = Field(default=90, ge=1, le=3650)


class ModelPolicyRequest(BaseModel):
    model_role: str = Field(default="REASONING", pattern="^(FAST|REASONING)$")
    model_id: str | None = Field(default=None, max_length=100)
    allow_external: bool = True
    allowed_classifications: list[str] = Field(default_factory=lambda: ["PUBLIC", "INTERNAL"], max_length=10)
    max_input_tokens: int = Field(default=24000, ge=1000, le=1000000)
    max_output_tokens: int = Field(default=3000, ge=100, le=100000)
    temperature: float = Field(default=0.1, ge=0, le=2)


def _organization(requested: str | None, principal: Principal) -> str:
    organization_id = requested or primary_organization(principal.user_id)
    if not organization_id: raise HTTPException(status_code=404, detail="No organization is available for this account")
    return organization_id


def _raise(exc: Exception) -> None:
    if isinstance(exc, PermissionError): raise HTTPException(status_code=403, detail={"code": "AI_POLICY_DENIED", "message": str(exc)}) from exc
    if isinstance(exc, KeyError): raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    if isinstance(exc, AIProviderError):
        status = 503 if exc.transient or exc.code in {"AI_PROVIDER_NOT_CONFIGURED", "AI_PROVIDER_UNAVAILABLE"} else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc), "transient": exc.transient}) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat")
async def chat(payload: ChatRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await ai_service.chat(_organization(organization_id, principal), principal.user_id, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.get("/conversations")
async def conversations(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.conversations(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.conversations(_organization(organization_id, principal), principal.user_id, conversation_id)
    except Exception as exc: _raise(exc)


@router.post("/controls/scan")
async def scan_controls(payload: ControlScanRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.generate_controls(_organization(organization_id, principal), principal.user_id, payload.repository_id)
    except Exception as exc: _raise(exc)


@router.get("/signals/sources")
async def signal_sources_catalog():
    return {"sources": signal_engine.list_sources()}


@router.get("/signals")
async def list_signals(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return {"signals": signal_engine.list_signal_definitions(_organization(organization_id, principal), principal.user_id)}
    except Exception as exc: _raise(exc)


@router.post("/signals", status_code=201)
async def create_signal(payload: CreateSignalRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return signal_engine.create_signal_definition(_organization(organization_id, principal), principal.user_id, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.patch("/signals/{signal_id}")
async def update_signal(signal_id: str, payload: UpdateSignalRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        return signal_engine.update_signal_definition(
            _organization(organization_id, principal), principal.user_id, signal_id,
            payload.model_dump(exclude_unset=True),
        )
    except Exception as exc: _raise(exc)


@router.delete("/signals/{signal_id}", status_code=204)
async def delete_signal(signal_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        signal_engine.delete_signal_definition(_organization(organization_id, principal), principal.user_id, signal_id)
        return None
    except Exception as exc: _raise(exc)


@router.get("/signals/{signal_id}/runs")
async def signal_runs(signal_id: str, limit: int = Query(default=30, ge=1, le=200), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return {"runs": signal_engine.signal_run_history(_organization(organization_id, principal), principal.user_id, signal_id, limit)}
    except Exception as exc: _raise(exc)


@router.post("/signals/scan")
async def scan_signals(payload: SignalScanRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try:
        return signal_engine.run_signal_scan(
            _organization(organization_id, principal), principal.user_id,
            repository_id=payload.repository_id, signal_id=payload.signal_id,
        )
    except Exception as exc: _raise(exc)


@router.post("/insights/{insight_id}/explain")
async def explain_insight(insight_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await signal_engine.explain_insight(_organization(organization_id, principal), principal.user_id, insight_id)
    except Exception as exc: _raise(exc)


@router.get("/insights")
async def insights(status: str = Query(default="OPEN", pattern="^(OPEN|CLOSED|DISMISSED)$"), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.insights(_organization(organization_id, principal), principal.user_id, status)
    except Exception as exc: _raise(exc)


@router.post("/insights/{insight_id}/recommendations", status_code=201)
async def create_recommendation(insight_id: str, payload: RecommendationRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.recommendation(_organization(organization_id, principal), principal.user_id, insight_id, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.post("/recommendations/{recommendation_id}/decision")
async def decide_recommendation(recommendation_id: str, payload: RecommendationDecision, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.decide_recommendation(_organization(organization_id, principal), principal.user_id, recommendation_id, payload.decision, payload.reason)
    except Exception as exc: _raise(exc)


@router.post("/agents/run", status_code=202)
async def run_agent(payload: AgentRunRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await ai_service.run_agent(_organization(organization_id, principal), principal.user_id, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.post("/agents/run/start", status_code=202)
async def start_agent(payload: AgentRunRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    """Background-launching twin of ``agents/run``: returns {agent_run_id,
    status} immediately so the caller can poll GET /agent-runs/{id} for
    live progress instead of blocking on the full investigation."""
    try: return ai_service.start_agent(_organization(organization_id, principal), principal.user_id, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.get("/agents/runs")
async def agent_runs(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.agent_runs(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/agent-runs/{agent_run_id}")
async def agent_run_progress(agent_run_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    """Generic, pollable status+steps+result for ANY agent run (new or
    existing) -- the single endpoint every agent's live-progress UI polls,
    regardless of which domain table its final answer actually lives in."""
    organization = _organization(organization_id, principal)
    try:
        can_audit = authorization_engine.authorize(
            principal.user_id, "ai.audit", ResourceContext("ORGANIZATION", organization, organization_id=organization),
        ).allowed
        return agent_runtime.get_run(organization, principal.user_id, agent_run_id, can_audit=can_audit)
    except Exception as exc: _raise(exc)


@router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return await ai_service.confirm_action(_organization(organization_id, principal), principal.user_id, action_id)
    except Exception as exc: _raise(exc)


@router.get("/usage")
async def usage(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.usage(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/ledger/agents")
async def ledger_agents(days: int = Query(default=30, ge=1, le=365), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return {"agents": ledger_analytics.agent_leaderboard(_organization(organization_id, principal), principal.user_id, days)}
    except Exception as exc: _raise(exc)


@router.get("/ledger/trend")
async def ledger_trend(days: int = Query(default=30, ge=1, le=365), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return {"trend": ledger_analytics.daily_trend(_organization(organization_id, principal), principal.user_id, days)}
    except Exception as exc: _raise(exc)


@router.get("/ledger/models")
async def ledger_models(days: int = Query(default=30, ge=1, le=365), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return {"models": ledger_analytics.model_breakdown(_organization(organization_id, principal), principal.user_id, days)}
    except Exception as exc: _raise(exc)


@router.get("/ledger/runs")
async def ledger_runs(
    agent_key: str | None = Query(default=None), status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200), cursor: int = Query(default=0, ge=0),
    organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal),
):
    try: return ledger_analytics.run_ledger(_organization(organization_id, principal), principal.user_id, agent_key, status, limit, cursor)
    except Exception as exc: _raise(exc)


@router.get("/ledger/runs/{agent_run_id}")
async def ledger_run_receipt(agent_run_id: str, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ledger_analytics.run_receipt(_organization(organization_id, principal), principal.user_id, agent_run_id)
    except Exception as exc: _raise(exc)


@router.get("/ledger/budget")
async def ledger_budget(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ledger_analytics.budget_status(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.post("/evaluations/run")
async def run_evaluation(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.run_evaluation(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/evaluations")
async def evaluations(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.evaluations(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.get("/administration")
async def administration(organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.administration(_organization(organization_id, principal), principal.user_id)
    except Exception as exc: _raise(exc)


@router.put("/administration/settings")
async def update_settings(payload: OrganizationAISettings, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.update_settings(_organization(organization_id, principal), principal.user_id, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.put("/administration/model-policies/{feature}")
async def update_model_policy(feature: str, payload: ModelPolicyRequest, organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return ai_service.update_model_policy(_organization(organization_id, principal), principal.user_id, feature, payload.model_dump())
    except Exception as exc: _raise(exc)


@router.get("/personal-activity")
async def get_personal_activity(range: str = Query(default="30d", pattern="^(7d|30d|all)$"), organization_id: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    try: return personal_activity(_organization(organization_id, principal), principal.user_id, range)
    except Exception as exc: _raise(exc)
