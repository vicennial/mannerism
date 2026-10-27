"""A tiny JSON-Schema subset validator for work-packet results.

Supports: type (object, array, string, integer, number, boolean), properties, required,
additionalProperties (false), items, enum, minimum, maximum, minLength, maxLength, minItems, maxItems.
Enough for the packet schemas; not a general validator. Returns a list of human-readable errors.
"""
from __future__ import annotations

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def _is(value, typ: str) -> bool:
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[typ])


def validate(value, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    typ = schema.get("type")
    if typ and not _is(value, typ):
        return [f"{path}: expected {typ}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if len(value) > schema.get("maxLength", 10**9):
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if len(value) > schema.get("maxItems", 10**9):
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field {req!r}")
        if schema.get("additionalProperties") is False:
            for extra in sorted(set(value) - set(props)):
                errors.append(f"{path}: unexpected field {extra!r}")
        for key, sub in props.items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    return errors
