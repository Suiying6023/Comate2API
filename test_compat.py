"""Focused regressions for native tool fragments, continuation and JSON validation."""

import base64
import json
import unittest

import comate2api as api
import openai_compat as compat
import schema_utils as schemas
from riskguard import CircuitBreaker, ClientFingerprint, CredentialPool, TokenBucket, UsageTracker


WEATHER_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}},
                  "required": ["city"], "additionalProperties": False}
TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": WEATHER_SCHEMA}}]


def request(**fields):
    return compat.CompletionRequest({"messages": [{"role": "user", "content": "Weather in Singapore?"}], **fields})


def native_call(accumulator, cid="call_test", name="UseMcpTool", params=None):
    def event(kind, data):
        accumulator.consume({"type": kind, "detail": {"toolUse": [{"id": cid, "name": name, "input": data}]}})
    event("FUNCTION_CALL_START", {})
    for key, value in (params or {"server_name": compat.MCP_SERVER, "tool_name": "get_weather",
                                 "arguments": '{"city":"Singapore"}'}).items():
        event("FUNCTION_CALL_PARAMS_APPEND", {key: value[:len(value) // 2]})
        event("FUNCTION_CALL_PARAMS_APPEND", {key: value[len(value) // 2:]})
        event("FUNCTION_CALL_PARAMS_APPEND", {key: ""})
    event("FUNCTION_CALL_END", {})


class CompatibilityTests(unittest.TestCase):
    def test_mcp_fragments_and_repeated_end(self):
        accumulator = compat.NativeTools()
        native_call(accumulator)
        accumulator.consume({"type": "FUNCTION_CALL_END", "detail": {"toolUse": [
            {"id": "call_test", "name": "UseMcpTool", "input": {"arguments": "ignored duplicate"}}]}})
        message, finish, bindings = accumulator.finish(request(tools=TOOLS, tool_choice="required"), "", "")
        self.assertEqual(finish, "tool_calls")
        self.assertIsNone(message["content"])
        call = message["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "get_weather")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "Singapore"})
        self.assertEqual(bindings[0]["native"]["params"]["arguments"], '{"city":"Singapore"}')

    def test_typed_nested_structured_output(self):
        schema = {"type": "object", "$defs": {"Item": {"type": "object", "properties": {
            "quantity": {"type": "integer"}, "code": {"type": "string"}},
            "required": ["quantity", "code"], "additionalProperties": False}},
            "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}},
                           "active": {"type": "boolean"}, "missing": {"type": "null"}},
            "required": ["items", "active", "missing"], "additionalProperties": False}
        accumulator = compat.NativeTools()
        native_call(accumulator, name="structured_output", params={
            "items": '[{"quantity":"7","code":"001"}]', "active": "false", "missing": "null"})
        plan = request(response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}})
        message, finish, bindings = accumulator.finish(plan, "", "")
        self.assertEqual(finish, "stop")
        self.assertEqual(bindings, [])
        self.assertEqual(json.loads(message["content"]), {"items": [{"quantity": 7, "code": "001"}], "active": False, "missing": None})

    def test_json_object_preserves_json_types(self):
        plan = request(response_format={"type": "json_object"})
        self.assertNotIn("structured_output", plan.agent_info["toolNames"])
        message, _, _ = compat.NativeTools().finish(plan, '{"count":2,"code":"001","active":false}', "")
        self.assertEqual(json.loads(message["content"]), {"count": 2, "code": "001", "active": False})

    def test_schema_constraints_and_boolean_number_distinction(self):
        schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 2},
                  "code": {"type": "string", "pattern": "^[A-Z]{2}$"}}, "required": ["count", "code"], "additionalProperties": False}
        schemas.check_schema(schema)
        schemas.validate({"count": 2, "code": "OK"}, schema)
        for value in ({"count": True, "code": "OK"}, {"count": 1, "code": "OK"},
                      {"count": 2, "code": "no"}, {"count": 2, "code": "OK", "extra": 1}):
            with self.assertRaises(ValueError):
                schemas.validate(value, schema)
        with self.assertRaises(ValueError):
            schemas.validate(True, {"enum": [1]})
        self.assertEqual(schemas.normalize_native("001", {"const": "001"}), "001")

    def test_schema_refs_formats_and_unsupported_assertions(self):
        schemas.check_schema({"type": "string", "format": "date-time"})
        schemas.validate("2026-11-13T11:30:00Z", {"type": "string", "format": "date-time"})
        with self.assertRaises(ValueError):
            schemas.validate("2026-99-99T11:30:00Z", {"type": "string", "format": "date-time"})
        with self.assertRaises(ValueError):
            schemas.check_schema({"$ref": "#/$defs/missing"})
        with self.assertRaises(ValueError):
            schemas.check_schema({"dependentSchemas": {}})
        with self.assertRaises(ValueError):
            schemas.check_schema({"pattern": "["})

    def test_required_none_and_parallel_choices(self):
        with self.assertRaises(ValueError):
            compat.NativeTools().finish(request(tools=TOOLS, tool_choice="required"), "Text instead", "")
        disabled = request(tools=TOOLS, tool_choice="none")
        self.assertEqual(disabled.active_tools, {})
        accumulator = compat.NativeTools()
        native_call(accumulator, cid="call_one")
        native_call(accumulator, cid="call_two")
        with self.assertRaises(ValueError):
            accumulator.finish(request(tools=TOOLS, parallel_tool_calls=False), "", "")

    def test_incomplete_and_unknown_tool_calls_fail(self):
        accumulator = compat.NativeTools()
        accumulator.consume({"type": "FUNCTION_CALL_START", "detail": {"toolUse": [{"id": "call_test", "name": "UseMcpTool", "input": {}}]}})
        with self.assertRaises(ValueError):
            accumulator.finish(request(tools=TOOLS), "", "")
        accumulator = compat.NativeTools()
        native_call(accumulator, params={"server_name": compat.MCP_SERVER, "tool_name": "undeclared", "arguments": "{}"})
        with self.assertRaises(ValueError):
            accumulator.finish(request(tools=TOOLS), "", "")

    def test_native_session_round_trip_and_restart_fallback(self):
        plan = request(tools=TOOLS)
        accumulator = compat.NativeTools()
        native_call(accumulator)
        message, _, bindings = accumulator.finish(plan, "", "")
        pool = CredentialPool([{"license": "unit-test-credential"}])
        scheduler = api.Scheduler(pool, TokenBucket(rate=2, capacity=2), CircuitBreaker(), UsageTracker(), ClientFingerprint())
        plan.credential_id = next(iter(pool.creds))
        scheduler.remember_tools(plan, "model-a", 123, 456, bindings)
        follow = compat.CompletionRequest({"messages": plan.messages + [message,
            {"role": "tool", "tool_call_id": "call_test", "content": "23 C"}], "tools": TOOLS})
        follow.resume = scheduler.resume_for(follow, "model-a")
        self.assertEqual((follow.resume["conv_id"], follow.resume["task_id"]), (123, 456))
        result = scheduler.tool_results_for(follow)[0]
        self.assertEqual(result["id"], "call_test")
        self.assertEqual(result["name"], "use_mcp_tool")
        self.assertEqual(result["message"], "23 C")
        scheduler.remember_tools(follow, "model-a", 123, 456, [])
        self.assertIsNone(scheduler.resume_for(follow, "model-a"))

    def test_missing_parallel_result_rejected(self):
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
            for cid in ("call_one", "call_two")]}
        with self.assertRaises(compat.InputError):
            compat.CompletionRequest({"messages": [message, {"role": "tool", "tool_call_id": "call_one", "content": "result"}]})

    def test_legacy_function_messages(self):
        plan = request(functions=[{"name": "get_weather", "parameters": WEATHER_SCHEMA}], function_call={"name": "get_weather"})
        accumulator = compat.NativeTools()
        native_call(accumulator)
        message, finish, bindings = accumulator.finish(plan, "", "")
        self.assertEqual(finish, "function_call")
        self.assertNotIn("tool_calls", message)
        message["function_call"] = {"arguments": message["function_call"]["arguments"], "name": "get_weather"}
        follow = compat.CompletionRequest({"messages": plan.messages + [message,
            {"role": "function", "name": "get_weather", "content": "23 C"}]})
        self.assertEqual(follow.tool_results[0]["id"], bindings[0]["key"])

    def test_schema_root_shape(self):
        with self.assertRaises(compat.InputError):
            request(response_format={"type": "json_schema", "json_schema": {
                "name": "array_result", "schema": {"type": "array", "items": {"type": "integer"}}}})

    def test_image_data_url_and_http_url(self):
        url = "data:image/png;base64," + base64.b64encode(b"image-bytes").decode("ascii")
        plan = compat.CompletionRequest({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "image_url", "image_url": {"url": "http://127.0.0.1/test.png"}}]}]})
        self.assertEqual(plan.images[0]["data"], b"image-bytes")
        self.assertEqual(plan.images[1]["url"], "http://127.0.0.1/test.png")
        with self.assertRaises(compat.InputError):
            compat.CompletionRequest({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,invalid!"}}]}]})


if __name__ == "__main__":
    unittest.main()
