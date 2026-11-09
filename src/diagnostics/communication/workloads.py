"""Synthetic API inputs, independent references, and value-free output schemas.

Gold is read by the host verifier only. Schema generation uses request field
types without constructing an answer or reading a reference answer.
"""

from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
import json


PROFILES = ("signup", "documents", "mystery")
ROLE_NAMES = ("PLANNER", "DEVELOPER", "QA", "SECURITY")


def request_digest(value):
    # Fixtures contain safe numbers. A Struct's integral floats represent the
    # same JSON numbers; the common comparison implementation normalizes them.
    from diagnostics.communication.contracts import fingerprint
    return fingerprint(value)


@lru_cache(maxsize=3)
def _asset(profile, kind):
    if profile not in PROFILES or kind not in ("request", "golden"):
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


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def shape_only(value):
    """Field/type structure only: no enum, const or expected scalar values."""
    if isinstance(value, dict):
        return object_schema({k: shape_only(v) for k, v in value.items()})
    if isinstance(value, list):
        variants = []
        for item in value:
            candidate = shape_only(item)
            if candidate not in variants:
                variants.append(candidate)
        items = variants[0] if len(variants) == 1 else {"anyOf": variants} if variants else {"type": "string"}
        return {"type": "array", "items": items}
    if value is None:
        return {"type": ["string", "null"]}
    return {"type": {bool: "boolean", int: "integer", float: "number", str: "string"}[type(value)]}


def specification_schema(request, role):
    """Create output types directly; answer values must come from the model."""
    string = {"type": "string"}
    strings = {"type": "array", "items": string}

    def contract(name):
        if name == "PLANNER":
            return object_schema({"dependencyEdges": {"type": "array", "items": strings}, "decision": string})
        if name == "DEVELOPER":
            return object_schema({"files": shape_only(request["targetFiles"]),
                                  "policy": shape_only(request["domainPolicy"]), "implementationState": string})
        if name == "QA":
            cases = request["acceptanceCases"]
            return object_schema({"caseIds": strings,
                                  "expectedOutcomes": object_schema({c["id"]: shape_only(c["expected"]) for c in cases}),
                                  "requirementByCase": object_schema({c["id"]: string for c in cases}),
                                  "executionState": string})
        if name == "SECURITY":
            return object_schema({"accessMatrix": shape_only(request["accessMatrix"]),
                                  "executionState": string, "deploymentState": string})
        raise ValueError("UNKNOWN_PROBE_ROLE")

    role_schema = contract(role) if role != "SINGLE_AGENT" else object_schema({
        "roles": object_schema({name: contract(name) for name in ROLE_NAMES})})
    return object_schema({
        "fixtureId": string, "coverageIds": strings,
        "constraints": shape_only(request["constraints"]), "unresolvedQuestionIds": strings,
        "accessMatrix": shape_only(request["accessMatrix"]),
        "executionEvidence": object_schema({k: string for k in ("build", "qa", "security", "deployment")}),
        "roleContract": role_schema,
    })


def fixture_statistics(profile):
    doc = load_request(profile)
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    return {"profile": profile, "requirements": len(doc["requirements"]),
            "cases": len(doc["acceptanceCases"]), "accessRules": len(doc["accessMatrix"]),
            "questions": len(doc["openQuestions"]), "briefCharacters": len(doc["brief"]),
            "fixtureBytes": len(raw), "fingerprint": request_digest(doc)}
