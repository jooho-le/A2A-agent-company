"""Bounded project JSON comparison, not an RFC signature/canonicalization."""

import hashlib
import json
import math


def normalized(value, _depth=0):
    if _depth > 64:
        raise ValueError("JSON_FINGERPRINT_INVALID")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("JSON_FINGERPRINT_INVALID")
        return {key: normalized(item, _depth + 1) for key, item in value.items()}
    if type(value) is list:
        return [normalized(item, _depth + 1) for item in value]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON_FINGERPRINT_INVALID")
        return int(value) if value.is_integer() else value
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("JSON_FINGERPRINT_INVALID")


def json_fingerprint(value):
    invalid = False
    try:
        data = json.dumps(normalized(value), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > 1_048_576:
            raise ValueError
        result = hashlib.sha256(data).hexdigest()
    except Exception:
        invalid = True
    if invalid:
        raise ValueError("JSON_FINGERPRINT_INVALID") from None
    return result
