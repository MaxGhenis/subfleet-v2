"""A small JSON Schema checker for the pinned provider schemas (tests only).

Covers what the generated Codex schemas use: `$ref` into `definitions`,
`type` (including lists and "null"), `enum`, `const`, `required`,
`properties`, `additionalProperties: false`, `items`, `oneOf`/`anyOf`/`allOf`,
and `minLength`. Formats and numeric bounds are ignored.
"""

from __future__ import annotations

from typing import Any

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def errors(value: Any, schema: Any, root: dict, path: str = "$") -> list[str]:
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{path}: nothing is allowed here"]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return errors(value, root["definitions"][name], root, path)
    out: list[str] = []
    for key in ("allOf",):
        for sub in schema.get(key, []):
            out += errors(value, sub, root, path)
    if "anyOf" in schema and not any(not errors(value, sub, root, path) for sub in schema["anyOf"]):
        out.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not errors(value, sub, root, path))
        if matches != 1:
            out.append(f"{path}: matches {matches} of oneOf")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, n) for n in names):
            return out + [f"{path}: {type(value).__name__} is not {names}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: {value!r} != {schema['const']!r}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        out.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                out += errors(value[key], sub, root, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                out.append(f"{path}: unexpected {sorted(extra)}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += errors(item, schema["items"], root, f"{path}[{i}]")
    return out
