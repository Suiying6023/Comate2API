#!/usr/bin/env python3
"""用本机服务跑一次非流式和一次流式模型调用；不输出凭证。"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="服务地址，默认读取 connection.json")
    parser.add_argument("--model", default="Deepseek V4 Flash")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    data_dir = Path(os.environ.get("COMATE_DATA_DIR") or Path(__file__).resolve().parent)
    info_file = data_dir / "connection.json"
    info = json.loads(info_file.read_text(encoding="utf-8-sig")) if info_file.exists() else {}
    urls = info.get("urls") or []
    local_urls = [url for url in urls if "127.0.0.1" in url or "localhost" in url]
    base = (args.base_url or (local_urls or urls or ["http://127.0.0.1:8181"])[0]).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    token = os.environ.get("COMATE_API_TOKEN", "").strip() or info.get("api_key", "")
    if not token:
        raise ValueError("请先启动服务，或设置 COMATE_API_TOKEN")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, payload=None, auth=True):
        headers = {"Authorization": "Bearer " + token} if auth else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return opener.open(urllib.request.Request(base + path, data=body, headers=headers), timeout=args.timeout)

    with request("/health", auth=False) as response:
        assert json.load(response)["status"] == "ok", "健康检查失败"
    try:
        with request("/v1/models", auth=False):
            raise AssertionError("模型接口未启用鉴权")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, f"鉴权状态码应为 401，实际 {exc.code}"
        exc.close()
    with request("/v1/models") as response:
        models = json.load(response)["data"]
        assert models, "模型列表为空"
    print(f"[PASS] 健康检查 / 鉴权 / 模型列表（{len(models)} 个）", flush=True)

    for stream in (False, True):
        marker = "COMATE_STREAM_OK" if stream else "COMATE_E2E_OK"
        payload = {"model": args.model, "stream": stream,
                   "messages": [{"role": "user", "content": f"Reply with exactly {marker}. Do not use tools."}]}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        with request("/v1/chat/completions", payload) as response:
            if not stream:
                result = json.load(response)
                content = result["choices"][0]["message"]["content"]
                usage = result.get("usage")
            else:
                content, chunks, usage, done = "", [], None, False
                for line in response:
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    chunk = json.loads(data)
                    assert "error" not in chunk, str(chunk.get("error"))
                    chunks.append(chunk)
                    if chunk["choices"]:
                        content += chunk["choices"][0]["delta"].get("content", "")
                    else:
                        usage = chunk.get("usage")
                assert done, "流式响应缺少 [DONE]"
                assert len({chunk["id"] for chunk in chunks}) == 1, "流 ID 不一致"
                assert len({chunk["created"] for chunk in chunks}) == 1, "流时间戳不一致"
                assert chunks[-1]["choices"] == [], "缺少尾部 usage 事件"
                assert chunks[-2]["choices"][0]["finish_reason"] == "stop", "结束原因不正确"
                # [DONE] 行之后还有 SSE 空行分隔符，读到 EOF 后应只剩空白。
                assert not response.read().strip(), "SSE 结束后还有额外内容"
            assert marker in content, f"回复缺少标记 {marker}"
            assert usage and usage["total_tokens"] > 0, "缺少真实 token 用量"
            assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"], "token 统计不一致"
            print(f"[PASS] {'流式' if stream else '非流式'}模型回复 / 用量（{usage['total_tokens']} tokens）", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, ValueError, KeyError, OSError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
