# Comate2API

把快码Comate账号凭证转成 OpenAI 兼容的对话 API。Python 3.8+，零第三方依赖。

- 流式 / 非流式对话、模型名称映射
- 工具调用与结果续接：`tools` / `tool_choice`，兼容 `functions`
- 图片输入：base64 data URL / HTTP(S) URL
- JSON / JSON Schema 输出（常用子集，返回前校验）
- 真实 token 用量、reasoning_content
- API Key 鉴权、限流、凭证冷却
- Windows、Linux、Docker

## 使用

本机已登录 Comate 会自动读取凭证；否则复制 `cred.example.json` 为 `cred.json`，填入自己的 license。

```bash
python start.py
```

Windows 也可双击 `启动服务.bat`，停止用 `停止服务.bat`。

客户端填写 `http://127.0.0.1:8181/v1`，API Key 在启动时显示。模型从 `/v1/models` 获取；向 `/v1/chat/completions` 发送：

```json
{"model":"Deepseek V4 Flash","messages":[{"role":"user","content":"你好"}],"stream":true}
```

Docker：

```bash
cp .env.example .env
# 在 .env 填入自己的 COMATE_LICENSE
docker compose up -d
```

运行 `python smoke_test.py` 验证流式 / 非流式调用（调用两次模型）。

工具和 JSON 响应先完整接收、校验，再以 SSE 返回。图片使用支持视觉的模型（已验证 GLM-5v-Turbo）。

音频、视频和文件暂未适配；采样、输出长度参数不透传。

[MIT License](LICENSE)
