# 凭证来源

启动器会自动读取本机已登录 Comate 的账号凭证。手动配置时，复制 `cred.example.json` 为 `cred.json`，填入自己的 license。

- Comate IDE：`%APPDATA%\Comate\User\settings.json` 的 `baidu.comate.license`。
- Comate CLI：`~/.comate/cli-login.json` 的 `license`。
- 导出：`python capture_credential.py --out cred.json`。

客户端访问本服务的 API Key 由启动器生成，保存于 `config.json`。

仓库与分享包只提供空凭证模板；`cred.json`、`.env`、`service.env`、运行配置和日志均不入库。
