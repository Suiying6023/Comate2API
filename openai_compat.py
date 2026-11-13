"""Translate OpenAI messages and native Comate MCP/structured-output calls."""

import base64
import hashlib
import html
import json
import urllib.parse

import schema_utils as schemas


MCP_SERVER = "openai_client"
AGENT_VERSION = 12


class InputError(ValueError):
    pass


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def legacy_key(call):
    function = call.get("function", call)
    canonical = json.dumps(function, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "legacy_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_tool(name):
    return {"UseMcpTool": "use_mcp_tool", "StructuredOutput": "structured_output"}.get(name, name)


class CompletionRequest:
    def __init__(self, body):
        self.owner = ""
        self.credential_id = None
        self.resume = None
        self.images = []
        self.tool_results = []
        self.history_calls = {}
        self.messages = body.get("messages")
        if not isinstance(self.messages, list) or not self.messages:
            raise InputError("messages 不能为空")
        self.legacy = bool(body.get("functions")) and not body.get("tools")
        if body.get("functions") and body.get("tools"):
            raise InputError("tools 和 functions 不能同时提供")
        raw_tools = body.get("tools")
        if raw_tools is None and self.legacy:
            raw_tools = [{"type": "function", "function": f} for f in body["functions"]]
        if raw_tools is None:
            raw_tools = []
        if not isinstance(raw_tools, list):
            raise InputError("tools 必须是数组")
        self.tools = {}
        for tool in raw_tools:
            if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                raise InputError("目前只支持 function 类型的 tools")
            function = dict(tool["function"])
            name = function.get("name")
            if not isinstance(name, str) or not name.strip() or name in self.tools:
                raise InputError("工具名称必须是非空且不重复的字符串")
            parameters = function.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(parameters, dict):
                raise InputError("工具 parameters 必须是 JSON Schema 对象")
            if parameters.get("type", "object") != "object":
                raise InputError("工具 parameters 的根节点必须是 object")
            if function.get("strict"):
                schemas.check_schema(parameters)
            function["parameters"] = parameters
            self.tools[name] = function
        self.parallel = body.get("parallel_tool_calls", True) and not self.legacy
        if not isinstance(body.get("parallel_tool_calls", True), bool):
            raise InputError("parallel_tool_calls 必须是布尔值")
        choice = body.get("tool_choice", body.get("function_call"))
        self.choice = choice if choice is not None else ("auto" if self.tools else "none")
        self.forced_name = None
        if isinstance(self.choice, dict):
            function = self.choice.get("function", self.choice)
            self.forced_name = function.get("name") if isinstance(function, dict) else None
            if self.forced_name not in self.tools:
                raise InputError("tool_choice 指定的工具不存在")
            self.choice = "required"
        elif self.choice not in ("auto", "none", "required"):
            raise InputError("tool_choice 必须是 auto、none、required 或指定函数")
        if self.choice == "required" and not self.tools:
            raise InputError("tool_choice=required 需要至少一个工具")
        self.active_tools = {} if self.choice == "none" else {
            name: f for name, f in self.tools.items() if not self.forced_name or name == self.forced_name
        }
        self.response_schema = None
        self.response_name = "response"
        self.json_object = False
        response_format = body.get("response_format")
        if response_format is not None:
            if not isinstance(response_format, dict):
                raise InputError("response_format 必须是对象")
            kind = response_format.get("type")
            if kind == "json_object":
                self.response_schema = {"type": "object"}
                self.json_object = True
            elif kind == "json_schema":
                config = response_format.get("json_schema")
                if not isinstance(config, dict) or not isinstance(config.get("schema"), dict):
                    raise InputError("json_schema.schema 必须是对象")
                if not isinstance(config.get("name"), str) or not config["name"]:
                    raise InputError("json_schema.name 必须是非空字符串")
                self.response_name = config["name"]
                self.response_schema = config["schema"]
                schemas.check_schema(self.response_schema)
                root_schema = schemas.resolved(self.response_schema, self.response_schema)
                if isinstance(root_schema, dict) and root_schema.get("type", "object") != "object":
                    raise InputError("json_schema 的根节点目前必须是 object")
            elif kind != "text":
                raise InputError("response_format.type 必须是 text、json_object 或 json_schema")
        self.query = self._messages()
        self.agent_info = self._agent_info()

    @property
    def buffered(self):
        return bool(self.active_tools or self.response_schema is not None or self.tool_results)

    def _content(self, content, allow_empty=False):
        if content is None and allow_empty:
            return ""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            raise InputError("message.content 必须是字符串或内容块数组")
        parts = []
        for part in content:
            if not isinstance(part, dict):
                raise InputError("内容块必须是对象")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif part.get("type") == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else None
                if not isinstance(url, str) or not url:
                    raise InputError("image_url.url 必须是非空字符串")
                record = {"url": url, "data": None, "mime": None}
                if url.startswith("data:"):
                    try:
                        header, data = url.split(",", 1)
                        mime, encoding = header[5:].split(";", 1)
                        if not mime.startswith("image/") or encoding != "base64":
                            raise ValueError()
                        record.update(data=base64.b64decode(data, validate=True), mime=mime)
                        if not record["data"]:
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise InputError("图片 data URL 必须包含有效的 image/* base64 数据")
                elif urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                    raise InputError("图片 URL 必须是 HTTP(S) 或 base64 data URL")
                self.images.append(record)
                parts.append("[Attached image %d]" % len(self.images))
            else:
                raise InputError("目前内容块支持 text 和 image_url；音频、视频和文件尚未适配")
        return "\n".join(parts)

    def _messages(self):
        parts, records = [], []
        for message in self.messages:
            if not isinstance(message, dict):
                raise InputError("messages 的每一项必须是对象")
            role = message.get("role") or "user"
            if role not in ("system", "developer", "user", "assistant", "tool", "function"):
                raise InputError("不支持的消息角色: " + str(role))
            calls = message.get("tool_calls") or []
            if message.get("function_call"):
                if not isinstance(message["function_call"], dict):
                    raise InputError("function_call 必须是对象")
                calls = [{"id": legacy_key(message["function_call"]), "type": "function", "function": message["function_call"]}]
            if calls and (role != "assistant" or not isinstance(calls, list)):
                raise InputError("tool_calls 只能出现在 assistant 消息中")
            parsed = {}
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise InputError("tool_calls 格式无效")
                function = call["function"]
                if not isinstance(call.get("id"), str) or not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str):
                    raise InputError("工具历史消息需要 id、name 和字符串 arguments")
                try:
                    arguments = schemas.loads(function["arguments"])
                except ValueError:
                    raise InputError("工具历史 arguments 必须是合法 JSON")
                parsed[call["id"]] = {"name": function["name"], "arguments": arguments}
            text = self._content(message.get("content"), allow_empty=bool(calls))
            result_id = message.get("tool_call_id") if role == "tool" else None
            if role == "function":
                previous = next((r["calls"] for r in reversed(records) if r["calls"]), {})
                result_id = next((cid for cid, c in previous.items() if c["name"] == message.get("name")), None)
            if role in ("tool", "function") and (not isinstance(result_id, str) or not result_id):
                raise InputError("工具结果需要对应的 tool_call_id 或 function.name")
            if text.strip():
                parts.append("%s%s: %s" % (role, " [" + result_id + "]" if result_id else "", text.strip()))
            if calls:
                parts.append("assistant tool_calls: " + json_text(calls))
            records.append({"role": role, "calls": parsed, "id": result_id, "content": text})
        tail = []
        for record in reversed(records):
            if record["role"] not in ("tool", "function"):
                self.history_calls = record["calls"]
                break
            tail.append({"id": record["id"], "content": record["content"]})
        if tail:
            self.tool_results = list(reversed(tail))
            ids = [r["id"] for r in tail]
            if len(set(ids)) != len(ids) or set(ids) != set(self.history_calls):
                raise InputError("必须为上一条 assistant 消息的每个工具调用提供一个对应结果")
        query = "\n\n".join(parts)
        if not query.strip():
            raise InputError("messages 里没有可用内容")
        return query

    def _agent_info(self):
        prompt = [
            "You are a conversational API assistant. The query contains the full message history with role labels. "
            "Follow system and developer messages, then answer the latest user request. "
            "Only use the tools explicitly enabled below. Tool execution is performed by the caller; never invent tool results."
        ]
        names = []
        info = {"agentName": "API", "isProjectAgent": True, "isCustomAgent": True, "canInvokeAgents": False}
        if self.active_tools:
            names.append("use_mcp_tool")
            descriptions = []
            for name, function in self.active_tools.items():
                descriptions.append('<server_tool name="%s"><tool_description>%s</tool_description><input_schema>%s</input_schema></server_tool>' % (
                    html.escape(name, quote=True), html.escape(str(function.get("description", ""))),
                    json_text(function["parameters"]).replace("<", "\\u003c").replace(">", "\\u003e")))
            prompt.append('Call the following functions using UseMcpTool / use_mcp_tool with server_name="%s", '
                          'tool_name set to the function name, and arguments containing a JSON object.\n<server name="%s">%s</server>'
                          % (MCP_SERVER, MCP_SERVER, "\n".join(descriptions)))
            info["mcpInfos"] = [{"serverName": MCP_SERVER, "tools": [
                {"name": name, "description": f.get("description", ""), "inputSchema": f["parameters"]}
                for name, f in self.active_tools.items()
            ]}]
            if self.choice == "required":
                prompt.append("You MUST call at least one of these functions in this response before providing a final answer.")
            else:
                prompt.append("Call a function when needed; otherwise answer directly. Existing tool results are already in the history.")
            if not self.parallel:
                prompt.append("Call at most one function in this response.")
        if self.json_object:
            prompt.append("When ready to answer, return exactly one valid JSON object as text. No prose or code fences. "
                          "Keep numbers, booleans, arrays and null as JSON values, not quoted strings.")
        elif self.response_schema is not None:
            names.append("structured_output")
            info["dynamicToolDefinitions"] = [{"name": "structured_output", "description": "Return the final JSON result: " + self.response_name, "inputSchema": self.response_schema}]
            prompt.append("When ready to provide the final answer, you MUST call structured_output exactly once with the requested JSON result. "
                          "Its input schema defines the required shape. Do not put the final answer in prose or a code fence. "
                          "If you need a caller tool's result, call that tool first and wait for its result.")
        if not names:
            prompt.append("Respond only with text. No tools are available.")
        info.update(toolNames=names, agentPrompt="\n\n".join(prompt))
        return info


class NativeTools:
    def __init__(self):
        self.calls = {}

    def consume(self, content):
        kind = content.get("type", "")
        if not kind.startswith(("FUNCTION_CALL_", "TOOL_CALL_")):
            return
        for tool in (content.get("detail") or {}).get("toolUse") or []:
            cid, name = tool.get("id"), canonical_tool(tool.get("name"))
            if not isinstance(cid, str) or not isinstance(name, str):
                raise ValueError("上游工具事件缺少 id 或 name")
            call = self.calls.setdefault(cid, {"id": cid, "name": name, "params": {}, "ended": False})
            if call["ended"]:
                continue
            for key, value in (tool.get("input") or {}).items():
                previous = call["params"].get(key, "")
                call["params"][key] = previous + value if "MERGE" not in kind and isinstance(previous, str) and isinstance(value, str) else value
            if kind.endswith("_END"):
                call["ended"] = True

    def finish(self, request, answer, reasoning):
        calls, bindings, structured = [], [], []
        for native in self.calls.values():
            if not native["ended"]:
                raise ValueError("上游工具参数未完整返回")
            if native["name"] == "structured_output" and request.response_schema is not None:
                value = schemas.normalize_native(native["params"], request.response_schema)
                schemas.validate(value, request.response_schema)
                structured.append(value)
                continue
            params = native["params"]
            name = params.get("tool_name")
            if native["name"] != "use_mcp_tool" or params.get("server_name") != MCP_SERVER or name not in request.active_tools:
                raise ValueError("上游调用了未声明或未启用的工具")
            arguments = params.get("arguments", "{}")
            arguments = schemas.loads(arguments or "{}") if isinstance(arguments, str) else arguments
            if not isinstance(arguments, dict):
                raise ValueError("工具 arguments 必须是 JSON 对象")
            schemas.validate(arguments, request.active_tools[name]["parameters"])
            call = {"id": native["id"], "type": "function", "function": {"name": name, "arguments": json_text(arguments)}}
            calls.append(call)
            bindings.append({"key": legacy_key(call) if request.legacy else call["id"], "call": call,
                             "native": {"id": native["id"], "name": native["name"], "params": dict(params)}})
        if calls and structured:
            raise ValueError("上游在工具结果返回前同时生成了最终结构化答案")
        if request.choice == "required" and not calls:
            raise ValueError("上游未遵循 tool_choice 的工具调用要求")
        if not request.parallel and len(calls) > 1:
            raise ValueError("上游未遵循单工具调用要求")
        if len(structured) > 1:
            raise ValueError("上游返回了多个结构化答案")
        if not calls and request.response_schema is not None:
            if structured:
                value = structured[0]
            else:
                text = answer.strip()
                if text.startswith("```") and text.endswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                value = schemas.loads(text)
                schemas.validate(value, request.response_schema)
            answer = json_text(value)
        message = {"role": "assistant", "content": answer or (None if calls else "")}
        finish = "stop"
        if calls:
            if request.legacy:
                message["function_call"] = calls[0]["function"]
                finish = "function_call"
            else:
                message["tool_calls"] = calls
                finish = "tool_calls"
        if reasoning:
            message["reasoning_content"] = reasoning
        return message, finish, bindings
