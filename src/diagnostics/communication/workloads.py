"""Fixed synthetic customer briefs, separate gold, and deterministic role mock.

Gold is read by the host's verifier only. `mock_specification` must not read the
gold file: it extracts a structured contract from the actual received request.
This tests data/contract grounding, not a real LLM's natural-language reasoning.
"""

from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
import json


PROFILES = ("basic", "signup", "documents", "mystery")
ROLE_NAMES = ("PLANNER", "DEVELOPER", "QA", "SECURITY")


def request_digest(value):
    # Fixtures contain safe numbers. A Struct's integral floats represent the
    # same JSON numbers; the common comparison implementation normalizes them.
    from diagnostics.communication.contracts import fingerprint
    return fingerprint(value)


@lru_cache(maxsize=3)
def _asset(profile, kind):
    if profile not in PROFILES[1:] or kind not in ("request", "golden"):
        raise ValueError("UNKNOWN_DIAGNOSTIC_FIXTURE")
    raw = files("diagnostics.communication").joinpath("fixtures", profile + "." + kind + ".json").read_text(encoding="utf-8")
    return json.loads(raw)


def load_request(profile):
    request = deepcopy(_asset(profile, "request"))
    if request_digest(request) != _asset(profile, "golden")["requestFingerprint"]:
        raise ValueError("FIXTURE_CHANGED_WITHOUT_REVIEWED_REFERENCE")
    return request


def expected_specification(profile, role):
    gold = _asset(profile, "golden")
    result = deepcopy(gold["common"])
    result["roleContract"] = deepcopy(gold["roles"][role]) if role != "SINGLE_AGENT" else {"roles": deepcopy(gold["roles"])}
    return result


def mock_specification(request, role):
    """Fixture-only role response generated without a reference answer."""
    requirements = request["requirements"]
    common = {
        "fixtureId": request["fixtureId"],
        "coverageIds": [item["id"] for item in requirements],
        "constraints": deepcopy(request["constraints"]),
        "unresolvedQuestionIds": [item["id"] for item in request["openQuestions"]],
        "requestFingerprint": request_digest(request),
        "accessMatrix": deepcopy(request["accessMatrix"]),
        "executionEvidence": {"build": "NOT_EXECUTED", "qa": "NOT_EXECUTED",
                              "security": "NOT_EXECUTED", "deployment": "NOT_REQUESTED"},
    }

    def contract(name):
        if name == "PLANNER":
            return {"dependencyEdges": [[dependency, item["id"]] for item in requirements for dependency in item["dependsOn"]],
                    "decision": "CLARIFICATION_REQUIRED" if request["openQuestions"] else "READY"}
        if name == "DEVELOPER":
            return {"files": deepcopy(request["targetFiles"]), "policy": deepcopy(request["domainPolicy"]),
                    "implementationState": "NOT_EXECUTED"}
        if name == "QA":
            cases = request["acceptanceCases"]
            return {"caseIds": [case["id"] for case in cases],
                    "expectedOutcomes": {case["id"]: deepcopy(case["expected"]) for case in cases},
                    "requirementByCase": {case["id"]: case["requirementId"] for case in cases},
                    "executionState": "NOT_EXECUTED"}
        if name == "SECURITY":
            return {"accessMatrix": deepcopy(request["accessMatrix"]), "executionState": "NOT_EXECUTED",
                    "deploymentState": "NOT_REQUESTED"}
        raise ValueError("UNKNOWN_PROBE_ROLE")

    common["roleContract"] = contract(role) if role != "SINGLE_AGENT" else {"roles": {name: contract(name) for name in ROLE_NAMES}}
    return common


def fixture_statistics(profile):
    if profile == "basic":
        return {"profile": profile, "requirements": 2, "cases": 0}
    doc = load_request(profile)
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    return {"profile": profile, "requirements": len(doc["requirements"]),
            "cases": len(doc["acceptanceCases"]), "accessRules": len(doc["accessMatrix"]),
            "questions": len(doc["openQuestions"]), "briefCharacters": len(doc["brief"]),
            "fixtureBytes": len(raw), "fingerprint": request_digest(doc)}
