# Comate2API

把百度文心快码（Comate）账号凭证转成 OpenAI 兼容的文本对话 API。Python 3.8+，零第三方依赖。

- 流式 / 非流式对话、模型名称映射
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

暂不支持工具调用、多模态和结构化输出；采样、输出长度参数不透传。

[MIT License](LICENSE)
