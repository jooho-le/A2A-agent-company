"""Discover trusted MVP IDs and acceptance criteria before submitting a Run."""

from fastapi import APIRouter

from orchestrator.domain.scenario_registry import SCENARIO_REGISTRY

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("")
def list_scenarios():
    return {"scenarios": [scenario.planner_contract() for scenario in SCENARIO_REGISTRY.values()]}
