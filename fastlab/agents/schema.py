"""Planner JSON parsing and the dependency-free schema subset validator."""

import json
import re

from .core import PlannerError


def parse_json_object(value):
    """Parse a planner response as a JSON object, accepting a JSON code fence."""
    text = str(value or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise PlannerError("Planner 没有返回 JSON。")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(
            "Planner 返回的 JSON 无法解析（第 %s 行第 %s 列）：%s。"
            % (exc.lineno, exc.colno, exc.msg)
        ) from exc
    if not isinstance(result, dict):
        raise PlannerError("Planner 必须返回一个 JSON 对象。")
    return result


def validate_json_schema(value, schema, path="$", root_schema=None):
    """Validate the draft-07 subset used by FastLab's bundled schemas.

    Keeping this validator in the standard-library adapter avoids making
    ``jsonschema`` a runtime dependency while still enforcing the same schema
    for every planner backend.
    """
    if not isinstance(schema, dict):
        return value
    root_schema = root_schema or schema
    if "$ref" in schema:
        reference = str(schema["$ref"])
        if not reference.startswith("#/"):
            raise PlannerError("Planner Schema 使用了不支持的引用：%s" % reference)
        target = root_schema
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return validate_json_schema(value, target, path, root_schema)

    expected = schema.get("type")
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(matches.get(item, False) for item in allowed):
            raise PlannerError("Planner JSON 的 %s 类型不符合 Schema。" % path)
    if "enum" in schema and value not in schema["enum"]:
        raise PlannerError(
            "Planner JSON 的 %s 只能是：%s。"
            % (path, ", ".join(map(str, schema["enum"])))
        )

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise PlannerError(
                "Planner JSON 的 %s 缺少字段：%s。" % (path, ", ".join(missing))
            )
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise PlannerError(
                    "Planner JSON 的 %s 包含未知字段：%s。"
                    % (path, ", ".join(extra))
                )
        for key, item in value.items():
            if key in properties:
                validate_json_schema(
                    item, properties[key], "%s.%s" % (path, key), root_schema
                )
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise PlannerError("Planner JSON 的 %s 项目过少。" % path)
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise PlannerError("Planner JSON 的 %s 项目过多。" % path)
        for index, item in enumerate(value):
            validate_json_schema(
                item,
                schema.get("items") or {},
                "%s[%s]" % (path, index),
                root_schema,
            )
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise PlannerError("Planner JSON 的 %s 不能为空。" % path)
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise PlannerError("Planner JSON 的 %s 过长。" % path)
        if schema.get("pattern") and not re.search(schema["pattern"], value):
            raise PlannerError("Planner JSON 的 %s 格式不符合 Schema。" % path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise PlannerError("Planner JSON 的 %s 小于允许值。" % path)
        if "maximum" in schema and value > schema["maximum"]:
            raise PlannerError("Planner JSON 的 %s 大于允许值。" % path)
    return value


__all__ = ["parse_json_object", "validate_json_schema"]


def strict_output_schema(schema):
    """Require nullable optional properties on the wire, retaining legacy input validation."""
    import copy
    result = copy.deepcopy(schema)
    def visit(node):
        if not isinstance(node, dict):
            return
        properties = node.get('properties')
        if properties is not None:
            original_required = set(node.get('required', []))
            node['required'] = list(properties)
            for key, value in properties.items():
                if key not in original_required and 'type' in value:
                    types = value['type'] if isinstance(value['type'], list) else [value['type']]
                    value['type'] = list(dict.fromkeys([*types, 'null']))
                visit(value)
        visit(node.get('items'))
    visit(result)
    return result
