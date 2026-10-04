"""A real evaluation harness for the AI gateway, run against a fixed,
versioned golden dataset of Git Walk domain questions.

This exists because AIService.run_evaluation() (see app/ai/service.py) —
despite living in a table called AI_EVALUATION_RUNS with a UI surface in AI
Command Center — is NOT a golden-dataset evaluation: its four "cases" are
synthetic self-checks (does JSON parsing work, does the injection regex
match a canned string, does a DB query return zero HIGH-risk tools without
explicit-confirmation) that never call the gateway, never touch real
evidence, and never exercise citation/grounding correctness at all. It's a
safety-baseline smoke test with an evaluation-shaped UI, not an evaluation
harness. This module is the real thing it was missing:

  - GOLDEN_CASES: 15-20 realistic (question, evidence, expected-answer-shape)
    tuples built from evidence shapes this product's own EvidenceRetriever
    actually produces (AUDIT_EVENT entries carrying commit_id/
    merge_request_id, REPOSITORY insights, EUC_FINDING, INTEGRATION_
    OPERATIONS) — not invented types.
  - A fake, per-case-scripted LLM provider (network calls are never made in
    tests) whose canned JSON responses exercise the REAL, untouched
    gateway.generate() grounding/citation-validation path: some cases cite
    correctly, one fabricates an extra reference to prove the gateway
    strips it, one has zero evidence to prove insufficient_evidence is
    forced True.
  - run_harness(): executes every case against the real AIGateway and
    returns a report asserting structured-output validity, that every
    surviving citation actually exists in the evidence passed in, and that
    insufficient_evidence matches each case's expectation.

Runnable two ways:
  - As a pytest case: backend/tests/test_ai_eval_harness.py
  - As a standalone script (for Phase 5 CI wiring): `python -m app.ai.eval_harness`
    — exits 0 on an all-pass run, 1 otherwise, and prints a per-case report.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from .gateway import AIGateway, ai_gateway
from .provider import LLMProvider, ProviderResult

GOLDEN_DATASET_VERSION = "2026-09-18.1"


def _evidence(type_: str, id_: str, **data: Any) -> dict[str, Any]:
    return {"type": type_, "id": id_, "title": data.pop("title", id_),
            "summary": data.pop("summary", ""), "data": data, "classification": "INTERNAL"}


def _answer_json(answer: str, evidence_refs: list[tuple[str, str]], *, insufficient: bool = False,
                  confidence: float = 0.85, warnings: list[str] | None = None) -> str:
    return json.dumps({
        "answer": answer,
        "evidence": [{"type": t, "id": i} for t, i in evidence_refs],
        "confidence": confidence,
        "insufficient_evidence": insufficient,
        "recommended_actions": [],
        "warnings": warnings or [],
    })


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    question: str
    evidence: list[dict[str, Any]]
    mock_response: str  # what the fake LLM "returns" — the thing under test is the gateway's handling of it
    expect_insufficient: bool
    expect_valid_evidence_ids: set[tuple[str, str]]
    notes: str = ""


GOLDEN_CASES: list[GoldenCase] = [
    GoldenCase(
        "commit_change_summary",
        "What changed in the latest commit to this repository?",
        [_evidence("AUDIT_EVENT", "AUD_C001", title="COMMIT CREATED",
                   event_type="COMMIT_CREATED", commit_id="CMT_1001", actor_user_id="USR_1",
                   event_payload={"changed_cells": 4, "sheet": "Budget"})],
        _answer_json("Commit CMT_1001 changed 4 cells on the Budget sheet.", [("AUDIT_EVENT", "AUD_C001")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_C001")},
    ),
    GoldenCase(
        "merge_request_approver",
        "Who approved merge request MR_5001?",
        [_evidence("AUDIT_EVENT", "AUD_M001", title="MERGE REQUEST APPROVED",
                   event_type="MERGE_REQUEST_APPROVED", merge_request_id="MR_5001", actor_user_id="USR_owner@example.com")],
        _answer_json("USR_owner@example.com approved merge request MR_5001.", [("AUDIT_EVENT", "AUD_M001")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_M001")},
    ),
    GoldenCase(
        "commit_history_multi",
        "Summarize commit activity in the last 24 hours.",
        [
            _evidence("AUDIT_EVENT", "AUD_C002", event_type="COMMIT_CREATED", commit_id="CMT_1002"),
            _evidence("AUDIT_EVENT", "AUD_C003", event_type="COMMIT_CREATED", commit_id="CMT_1003"),
            _evidence("AUDIT_EVENT", "AUD_C004", event_type="COMMIT_CREATED", commit_id="CMT_1004"),
        ],
        _answer_json("Three commits landed: CMT_1002, CMT_1003, CMT_1004.",
                     [("AUDIT_EVENT", "AUD_C002"), ("AUDIT_EVENT", "AUD_C003"), ("AUDIT_EVENT", "AUD_C004")]),
        expect_insufficient=False,
        expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_C002"), ("AUDIT_EVENT", "AUD_C003"), ("AUDIT_EVENT", "AUD_C004")},
    ),
    GoldenCase(
        "merge_conflict_detected",
        "Was there a conflict merging feature/pricing-update?",
        [_evidence("AUDIT_EVENT", "AUD_M002", event_type="MERGE_CONFLICT_DETECTED",
                   merge_request_id="MR_5002", event_payload={"conflicting_cells": 2})],
        _answer_json("Yes — merge request MR_5002 had 2 conflicting cells.", [("AUDIT_EVENT", "AUD_M002")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_M002")},
    ),
    GoldenCase(
        "validation_failures",
        "What validation failures happened on this repository?",
        [
            _evidence("REPOSITORY", "REPO_1", title="Sales Forecast", data_quality={"failed_runs": 2, "errors": 3}),
            _evidence("AUDIT_EVENT", "AUD_V001", event_type="VALIDATION_FAILED", status="FAILED"),
        ],
        _answer_json("Two validation runs failed with 3 errors overall; the most recent failure is logged as AUD_V001.",
                     [("REPOSITORY", "REPO_1"), ("AUDIT_EVENT", "AUD_V001")]),
        expect_insufficient=False, expect_valid_evidence_ids={("REPOSITORY", "REPO_1"), ("AUDIT_EVENT", "AUD_V001")},
    ),
    GoldenCase(
        "device_blocked_who_when",
        "Who blocked the device on this repository and when?",
        [_evidence("AUDIT_EVENT", "AUD_D001", event_type="DEVICE_BLOCKED",
                   actor_user_id="USR_owner", created_at="2026-09-10T12:00:00Z")],
        _answer_json("USR_owner blocked the device on 2026-09-10.", [("AUDIT_EVENT", "AUD_D001")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_D001")},
    ),
    GoldenCase(
        "repository_risk_posture",
        "What is the current risk posture of this repository?",
        [_evidence("REPOSITORY", "REPO_2", title="HR Roster", workflow={"conflict_rate": 12, "merge_success_rate": 88})],
        _answer_json("Merge success rate is 88% with a 12% conflict rate.", [("REPOSITORY", "REPO_2")]),
        expect_insufficient=False, expect_valid_evidence_ids={("REPOSITORY", "REPO_2")},
    ),
    GoldenCase(
        "euc_findings_explained",
        "Explain the open EUC findings for this workbook.",
        [
            _evidence("EUC_FINDING", "FND_1", title="Formula pattern break", severity="HIGH"),
            _evidence("EUC_FINDING", "FND_2", title="Hardcoded constant", severity="MEDIUM"),
        ],
        _answer_json("Two open findings: a HIGH-severity formula pattern break and a MEDIUM hardcoded constant.",
                     [("EUC_FINDING", "FND_1"), ("EUC_FINDING", "FND_2")]),
        expect_insufficient=False, expect_valid_evidence_ids={("EUC_FINDING", "FND_1"), ("EUC_FINDING", "FND_2")},
    ),
    GoldenCase(
        "commit_rollback_explained",
        "What happened during the last commit rollback?",
        [_evidence("AUDIT_EVENT", "AUD_R001", event_type="COMMIT_ROLLED_BACK", commit_id="CMT_1005")],
        _answer_json("Commit CMT_1005 was rolled back to the prior version.", [("AUDIT_EVENT", "AUD_R001")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_R001")},
    ),
    GoldenCase(
        "fabricated_citation_is_stripped",
        "What changed in commit CMT_2001?",
        [_evidence("AUDIT_EVENT", "AUD_C010", event_type="COMMIT_CREATED", commit_id="CMT_2001")],
        # The fake model cites one REAL evidence item and one that was never
        # in context — this is the case that proves gateway.generate()'s
        # citation-validation strips fabricated references instead of
        # trusting the model's own claim.
        _answer_json("Commit CMT_2001 updated 3 cells.",
                     [("AUDIT_EVENT", "AUD_C010"), ("AUDIT_EVENT", "AUD_NEVER_RETRIEVED")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_C010")},
        notes="Proves fabricated/unretrieved evidence references are dropped, not trusted.",
    ),
    GoldenCase(
        "no_evidence_is_insufficient",
        "What changed in a repository that was never provided as evidence?",
        [],
        _answer_json("I don't have information about that repository.", [], insufficient=True, confidence=0.0),
        expect_insufficient=True, expect_valid_evidence_ids=set(),
        notes="Zero evidence must force insufficient_evidence=True regardless of what the model claims.",
    ),
    GoldenCase(
        "model_honestly_admits_gap",
        "What is the SLA breach history for this repository over the last year?",
        [_evidence("REPOSITORY", "REPO_3", title="Ops Tracker", workflow={"merge_success_rate": 90})],
        # Evidence exists but doesn't answer the question — a well-behaved
        # model should say so rather than guess.
        _answer_json("The available evidence does not include SLA breach history.", [("REPOSITORY", "REPO_3")],
                     insufficient=True, confidence=0.2),
        expect_insufficient=True, expect_valid_evidence_ids={("REPOSITORY", "REPO_3")},
        notes="The model can correctly flag insufficient_evidence even when some evidence is present.",
    ),
    GoldenCase(
        "merge_requests_approved_this_week",
        "List merge requests approved this week.",
        [
            _evidence("AUDIT_EVENT", "AUD_M003", event_type="MERGE_REQUEST_APPROVED", merge_request_id="MR_5003"),
            _evidence("AUDIT_EVENT", "AUD_M004", event_type="MERGE_REQUEST_APPROVED", merge_request_id="MR_5004"),
        ],
        _answer_json("MR_5003 and MR_5004 were approved this week.",
                     [("AUDIT_EVENT", "AUD_M003"), ("AUDIT_EVENT", "AUD_M004")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_M003"), ("AUDIT_EVENT", "AUD_M004")},
    ),
    GoldenCase(
        "single_audit_event_actor",
        "Who is the actor recorded on audit event AUD_A100?",
        [_evidence("AUDIT_EVENT", "AUD_A100", event_type="ROLE_ASSIGNED", actor_user_id="USR_admin")],
        _answer_json("USR_admin is the recorded actor.", [("AUDIT_EVENT", "AUD_A100")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_A100")},
    ),
    GoldenCase(
        "device_trust_audit_trail",
        "What is the device-trust audit trail for this repository?",
        [
            _evidence("AUDIT_EVENT", "AUD_D002", event_type="DEVICE_TRUSTED"),
            _evidence("AUDIT_EVENT", "AUD_D003", event_type="DEVICE_BLOCKED"),
        ],
        _answer_json("One device was trusted (AUD_D002), then a different device was later blocked (AUD_D003).",
                     [("AUDIT_EVENT", "AUD_D002"), ("AUDIT_EVENT", "AUD_D003")]),
        expect_insufficient=False, expect_valid_evidence_ids={("AUDIT_EVENT", "AUD_D002"), ("AUDIT_EVENT", "AUD_D003")},
    ),
    GoldenCase(
        "integration_health_summary",
        "Summarize integration health for the organization.",
        [_evidence("INTEGRATION_OPERATIONS", "ORG_1", title="Integration operations",
                   connections={"healthy": 3, "degraded": 1})],
        _answer_json("3 integrations are healthy and 1 is degraded.", [("INTEGRATION_OPERATIONS", "ORG_1")]),
        expect_insufficient=False, expect_valid_evidence_ids={("INTEGRATION_OPERATIONS", "ORG_1")},
    ),
    GoldenCase(
        "all_evidence_fabricated",
        "What changed in commit CMT_9999?",
        [_evidence("AUDIT_EVENT", "AUD_REAL", event_type="COMMIT_CREATED", commit_id="CMT_OTHER")],
        # Every cited reference is fabricated (doesn't match the one real
        # item in context) — the surviving valid-evidence set must be empty,
        # and with zero valid citations against real evidence, grounding
        # confidence correctly falls below the gateway's own insufficient-
        # evidence threshold (retrieval_quality*.45 + citation_coverage*.55
        # < .35 when citation_coverage is 0) — this is the gateway's real
        # low-confidence path, not the zero-evidence shortcut.
        _answer_json("Commit CMT_9999 updated the pricing sheet.", [("AUDIT_EVENT", "AUD_FAKE_1"), ("AUDIT_EVENT", "AUD_FAKE_2")]),
        expect_insufficient=True, expect_valid_evidence_ids=set(),
        notes="All citations fabricated — every one must be stripped, and grounding confidence "
              "correctly drops low enough to force insufficient_evidence=True.",
    ),
]


class _ScriptedProvider(LLMProvider):
    """Returns one pre-scripted response per case — the fake stands in for
    the network call, never for the gateway's own validation logic, which
    runs for real against whatever this returns."""

    provider_name = "OPENROUTER"

    def __init__(self, response_text: str):
        self.response_text = response_text

    async def complete(self, **kwargs) -> ProviderResult:
        return ProviderResult(self.response_text, input_tokens=100, output_tokens=40, reasoning_tokens=0)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    answer: dict[str, Any] | None = None


@dataclass
class HarnessReport:
    dataset_version: str
    results: list[CaseResult]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def summary(self) -> str:
        ok = sum(1 for r in self.results if r.passed)
        return f"{ok}/{len(self.results)} cases passed (golden dataset {self.dataset_version})"


def _hash(value: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


async def run_case(organization_id: str, user_id: str, case: GoldenCase) -> CaseResult:
    gateway = AIGateway({"OPENROUTER": _ScriptedProvider(case.mock_response)})
    failures: list[str] = []
    try:
        result = await gateway.generate(
            organization_id=organization_id, user_id=user_id, feature="ENTERPRISE_COPILOT",
            question=case.question, evidence=case.evidence, context_hash=_hash(case.evidence),
        )
    except Exception as exc:  # noqa: BLE001 — a raised exception IS a failed case, not a harness bug
        return CaseResult(case.case_id, False, [f"generate() raised {type(exc).__name__}: {exc}"])

    # 1. Structured-output validity: reaching here at all means the gateway
    #    parsed a valid GroundedAnswer (or repaired one) — assert the shape
    #    explicitly rather than just trusting no-exception.
    for required in ("answer", "evidence", "confidence", "insufficient_evidence", "warnings"):
        if required not in result:
            failures.append(f"missing required field '{required}' in response")

    # 2. Grounding/citation correctness: every surviving citation must
    #    reference evidence actually present in context — this is the
    #    property gateway.generate() is supposed to guarantee; the harness
    #    verifies it holds, it doesn't just assume it.
    allowed = {(item["type"], item["id"]) for item in case.evidence}
    returned_refs = {(ref["type"], ref["id"]) for ref in result.get("evidence", [])}
    if not returned_refs.issubset(allowed):
        failures.append(f"citation(s) not present in evidence: {returned_refs - allowed}")
    if returned_refs != case.expect_valid_evidence_ids:
        failures.append(
            f"expected valid evidence {case.expect_valid_evidence_ids}, got {returned_refs}"
        )

    # 3. insufficient_evidence must match the case's expectation.
    if bool(result.get("insufficient_evidence")) != case.expect_insufficient:
        failures.append(
            f"expected insufficient_evidence={case.expect_insufficient}, got {result.get('insufficient_evidence')}"
        )

    return CaseResult(case.case_id, not failures, failures, result)


async def run_harness(organization_id: str, user_id: str) -> HarnessReport:
    results = [await run_case(organization_id, user_id, case) for case in GOLDEN_CASES]
    return HarnessReport(GOLDEN_DATASET_VERSION, results)


def _bootstrap_standalone_run() -> int:
    """Standalone-script entry point (Phase 5 CI wiring target):
    `python -m app.ai.eval_harness`. Builds its own temp DB, org, and user
    so it needs no external fixture, then prints a per-case report."""
    import tempfile
    from pathlib import Path

    from .. import database
    from ..secret_store import encrypt_secret
    from .service import AIService

    temp_dir = tempfile.TemporaryDirectory()
    original_db_path = database.DB_PATH
    database.DB_PATH = Path(temp_dir.name) / "eval_harness.db"
    try:
        database.initialize_product_schema()
        user = database.get_or_create_user("eval-harness@example.com")
        database.save_user_ai_settings(user["user_id"], encrypt_secret("sk-or-eval-harness"), "nvidia/nemotron-3-super-120b-a12b:free")
        organization_id = "ORG_EVAL_HARNESS"
        conn = database._get_connection()
        try:
            AIService._ensure_settings(conn, organization_id, user["user_id"])
            conn.commit()
        finally:
            conn.close()

        report = asyncio.run(run_harness(organization_id, user["user_id"]))
        print(report.summary)
        for result in report.results:
            status = "PASS" if result.passed else "FAIL"
            print(f"  [{status}] {result.case_id}")
            for failure in result.failures:
                print(f"         - {failure}")
        return 0 if report.passed else 1
    finally:
        database.DB_PATH = original_db_path
        temp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(_bootstrap_standalone_run())
