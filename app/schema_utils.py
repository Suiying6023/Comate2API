"""Standard-library validation for the common JSON Schema subset used by clients."""

import json
import math
import re
import datetime
import ipaddress
import urllib.parse
import uuid


TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
KEYWORDS = {
    "$ref", "$defs", "definitions", "$schema", "$id", "$comment",
    "title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly",
    "type", "properties", "patternProperties", "required", "additionalProperties",
    "minProperties", "maxProperties", "items", "prefixItems", "minItems", "maxItems",
    "uniqueItems", "enum", "const", "anyOf", "oneOf", "allOf", "not",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format",
}
FORMATS = {"date-time", "date", "time", "duration", "email", "hostname", "ipv4", "ipv6", "uuid", "uri", "uri-reference"}


def check_pattern(pattern):
    try:
        re.compile(pattern)
    except (re.error, TypeError):
        raise ValueError("Schema pattern 不是有效的正则表达式")


def valid_format(value, kind):
    try:
        if kind == "uuid":
            uuid.UUID(value)
            return bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value))
        if kind in ("ipv4", "ipv6"):
            return ipaddress.ip_address(value).version == (4 if kind == "ipv4" else 6)
        if kind == "date":
            datetime.date.fromisoformat(value)
            return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        if kind == "date-time":
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
            return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value)) and parsed.tzinfo is not None
        if kind == "time":
            parsed = datetime.time.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
            return bool(re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value)) and parsed.tzinfo is not None
        if kind == "duration":
            return bool(re.fullmatch(r"P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?|P\d+W", value))
        if kind == "email":
            return bool(re.fullmatch(r"[^\s@]+@[^\s@]+", value))
        if kind == "hostname":
            labels = value.rstrip(".").encode("idna").decode("ascii").split(".")
            return len(value) <= 253 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)
        if kind in ("uri", "uri-reference"):
            return not re.search(r"\s", value) and (kind == "uri-reference" or bool(urllib.parse.urlsplit(value).scheme))
    except (ValueError, UnicodeError):
        return False
    return False


def loads(text):
    def invalid_constant(value):
        raise ValueError("JSON 不允许 " + value)
    return json.loads(text, parse_constant=invalid_constant)


def reference(ref, root):
    if ref == "#":
        return root
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError("Schema 目前只支持本地 $ref")
    node = root
    try:
        for key in ref[2:].split("/"):
            node = node[key.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        raise ValueError("Schema $ref 不存在: " + ref)
    return node


def resolved(schema, root):
    seen = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            raise ValueError("Schema 存在循环的直接 $ref")
        seen.add(ref)
        schema = reference(ref, root)
    return schema


def check_schema(schema, root=None):
    """Reject unsupported assertions instead of promising validation we cannot do."""
    root = schema if root is None else root
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValueError("Schema 必须是对象或布尔值")
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError("暂未支持的 Schema 约束: " + ", ".join(sorted(unknown)))
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    if not isinstance(types, list) or any(t not in TYPES for t in types):
        raise ValueError("Schema type 无效")
    if "$ref" in schema:
        reference(schema["$ref"], root)
    for key in ("properties", "patternProperties", "$defs", "definitions"):
        if key in schema:
            if not isinstance(schema[key], dict):
                raise ValueError("Schema " + key + " 必须是对象")
            for name, child in schema[key].items():
                if key == "patternProperties":
                    check_pattern(name)
                check_schema(child, root)
    for key in ("items", "additionalProperties", "not"):
        if key in schema:
            check_schema(schema[key], root)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise ValueError("Schema " + key + " 必须是非空数组")
            for child in schema[key]:
                check_schema(child, root)
    if "required" in schema and (not isinstance(schema["required"], list)
                                 or any(not isinstance(k, str) for k in schema["required"])):
        raise ValueError("Schema required 必须是字符串数组")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("Schema enum 必须是非空数组")
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("Schema " + key + " 必须是非负整数")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError("Schema " + key + " 必须是有限数值")
    if "multipleOf" in schema and schema["multipleOf"] <= 0:
        raise ValueError("Schema multipleOf 必须大于零")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ValueError("Schema uniqueItems 必须是布尔值")
    if "pattern" in schema:
        check_pattern(schema["pattern"])
    if "format" in schema and schema["format"] not in FORMATS:
        raise ValueError("暂未支持的 Schema format: " + str(schema["format"]))


def matches_type(value, kind):
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind in ("number", "integer"):
        return (type(value) in (int, float) and math.isfinite(value)
                and (kind == "number" or int(value) == value))
    return False


def equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def validate(value, schema, root=None, path="$", depth=0):
    root = schema if root is None else root
    if depth > 128:
        raise ValueError(path + ": Schema 嵌套过深")
    if schema is True:
        return
    if schema is False:
        raise ValueError(path + ": Schema 不允许此值")
    if "$ref" in schema:
        validate(value, reference(schema["$ref"], root), root, path, depth + 1)
    kinds = schema.get("type")
    if kinds and not any(matches_type(value, t) for t in ([kinds] if isinstance(kinds, str) else kinds)):
        raise ValueError(path + ": 类型不符合 Schema")
    if "enum" in schema and not any(equal(value, item) for item in schema["enum"]):
        raise ValueError(path + ": 不在 enum 中")
    if "const" in schema and not equal(value, schema["const"]):
        raise ValueError(path + ": 不符合 const")
    for key in ("allOf", "anyOf", "oneOf", "not"):
        if key not in schema:
            continue
        children = [schema[key]] if key == "not" else schema[key]
        passed = 0
        for child in children:
            try:
                validate(value, child, root, path, depth + 1)
                passed += 1
            except ValueError:
                pass
        if ((key == "allOf" and passed != len(children)) or (key == "anyOf" and not passed)
                or (key == "oneOf" and passed != 1) or (key == "not" and passed)):
            raise ValueError(path + ": 不符合 " + key)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(path + ": 缺少字段 " + key)
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        for key, item in value.items():
            checks = ([properties[key]] if key in properties else []) + [s for p, s in patterns.items() if re.search(p, key)]
            if not checks:
                checks = [schema.get("additionalProperties", True)]
            for child in checks:
                validate(item, child, root, path + "." + key, depth + 1)
        lower, upper = schema.get("minProperties", 0), schema.get("maxProperties", math.inf)
        if not lower <= len(value) <= upper:
            raise ValueError(path + ": 字段数量不符合 Schema")
    if isinstance(value, list):
        prefix = schema.get("prefixItems", [])
        for index, item in enumerate(value):
            child = prefix[index] if index < len(prefix) else schema.get("items", True)
            validate(item, child, root, "%s[%s]" % (path, index), depth + 1)
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise ValueError(path + ": 数组长度不符合 Schema")
        if schema.get("uniqueItems") and any(equal(item, other) for i, item in enumerate(value) for other in value[:i]):
            raise ValueError(path + ": 数组元素重复")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise ValueError(path + ": 字符串长度不符合 Schema")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError(path + ": 字符串不符合 pattern")
        if schema.get("format") in FORMATS and not valid_format(value, schema["format"]):
            raise ValueError(path + ": 字符串不符合 format")
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError(path + ": 不允许非有限数值")
        if (value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf)
                or value <= schema.get("exclusiveMinimum", -math.inf)
                or value >= schema.get("exclusiveMaximum", math.inf)):
            raise ValueError(path + ": 数值范围不符合 Schema")
        if "multipleOf" in schema:
            quotient = value / schema["multipleOf"]
            if not math.isclose(quotient, round(quotient), rel_tol=1e-10, abs_tol=1e-10):
                raise ValueError(path + ": 不符合 multipleOf")


def normalize_native(value, schema, root=None):
    """Native tool events serialize each parameter independently as text."""
    root = schema if root is None else root
    schema = resolved(schema, root)
    if not isinstance(schema, dict):
        return value
    for child in schema.get("allOf", []):
        value = normalize_native(value, child, root)
    for key in ("anyOf", "oneOf"):
        for child in schema.get(key, []):
            candidate = normalize_native(value, child, root)
            try:
                validate(candidate, child, root)
                return candidate
            except ValueError:
                pass
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if isinstance(value, str) and "string" not in kinds:
        if kinds or "properties" in schema or "items" in schema or "const" in schema or "enum" in schema:
            try:
                # enum/const 可以只声明字符串；保留已经符合该约束的值。
                validate(value, schema, root)
            except ValueError:
                try:
                    value = loads(value)
                except ValueError:
                    pass
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        return {k: normalize_native(v, properties.get(k, schema.get("additionalProperties", {})), root)
                for k, v in value.items()}
    if isinstance(value, list):
        prefix = schema.get("prefixItems", [])
        return [normalize_native(v, prefix[i] if i < len(prefix) else schema.get("items", {}), root)
                for i, v in enumerate(value)]
    return value
