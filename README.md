# Baidu OpenAI Proxy

把 `https://chat.baidu.com` 网页端能力封装为 OpenAI 兼容协议，并提供后台维护接口参数、模型映射、凭证、提示词、日志和统计。

## 已实现接口

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/chat/completions` with `stream=true`
- `POST /v1/responses`
- `POST /v1/responses` with `stream=true`
- `POST /v1/images/generations`
- `POST /v1/files`

## 后台能力

- 系统配置维护
- 模型 ID 映射维护
- 百度 Cookie/凭证池维护、启停、健康检测
- API Key 创建、查看、启停、模型权限控制
- API Key 每日限额、全局分钟限流
- IP 白名单/黑名单
- 内置提示词开启、关闭、首次/每条模式
- 请求日志、系统日志、操作审计查看
- 请求日志 CSV 导出、按保留天数清理
- 配置导入/导出
- 后台密码修改

## 百度适配能力

- 首页 `aiTabFrameBaseData` 解析
- `chat_token` 生成
- `conversation` SSE 调用
- 文本、思考、图片、去水印、长文工作区组件解析
- `canvas/search` 工作区文件查询方法
- `download` Markdown/docx/pdf 导出方法

## 默认模型映射

| OpenAI 模型 ID | 百度 modelName | 说明 |
|---|---|---|
| `deepseek-v4-pro` | `DeepSeek-V4` | 默认开启 `thinkMode=1` |
| `deepseek-v4` | `DeepSeek-V4` | 默认关闭思考 |
| `ernie-5.1` | `ERINE-5.1` | 按当前抓包值 |
| `smart` | `smartMode` | 智能模式 |
| `miaotu` | `smartMode` | 出图路由 |

## 本地开发

```powershell
Copy-Item .env.example .env
.\scripts\run_dev.ps1
```

后台地址：

```text
http://127.0.0.1:8000/admin
```

默认后台密码来自 `.env` 的 `ADMIN_PASSWORD`。

## Linux 一键部署

```bash
bash scripts/install.sh --port 8000 --admin-password 'your-password'
```

也可以不带参数交互式安装：

```bash
sudo bash scripts/install.sh
```

如果已经在 `scripts/` 目录下，也可以运行：

```bash
sudo bash install.sh
```

多实例部署时，为每个项目目录选择不同端口即可。默认 systemd 服务名会按端口生成，例如：

```text
baidu-openai-proxy-8000.service
baidu-openai-proxy-8001.service
```

如需自定义服务名：

```bash
sudo bash scripts/install.sh --port 8001 --admin-password 'your-password' --service-name baidu-openai-proxy-8001
```

如只初始化项目、不安装开机自启服务：

```bash
bash scripts/install.sh --port 8000 --admin-password 'your-password' --no-service
```

脚本会：

- 创建 `.venv`
- 安装依赖
- 初始化 `.env`
- 初始化 SQLite 数据库
- 生成默认 API Key
- 创建并启动 systemd 服务，并执行 `systemctl enable` 支持开机自启

## Windows 一键部署

双击或在终端运行：

```bat
scripts\install_windows.bat
```

脚本会交互询问端口和后台密码，然后自动：

- 检测 Python 3
- 创建 `.venv`
- 安装 `requirements.txt`
- 初始化 `.env`
- 初始化 SQLite 数据库
- 生成默认 API Key
- 创建 Windows 计划任务并启动当前实例

也可以用参数非交互安装：

```powershell
.\scripts\install_windows.bat -Port 8000 -AdminPassword "your-password"
```

默认计划任务名按端口生成：

```text
BaiduOpenAIProxy-8000
BaiduOpenAIProxy-8001
```

多实例部署时，把项目放到不同目录，并使用不同端口：

```powershell
.\scripts\install_windows.bat -Port 8000 -AdminPassword "password-a"
.\scripts\install_windows.bat -Port 8001 -AdminPassword "password-b"
```

如只安装依赖和初始化配置，不创建开机自启任务：

```powershell
.\scripts\install_windows.bat -Port 8000 -AdminPassword "your-password" -NoStartup
```

如只初始化不立即启动：

```powershell
.\scripts\install_windows.bat -Port 8000 -AdminPassword "your-password" -NoStart
```

## OpenAI 调用示例

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-baidu-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

## 重要说明

当前版本是开发期网关骨架，已经接入百度网页端主链路，但稳定性仍依赖网页端参数和 Cookie 状态。生产部署前建议：

- 在后台配置有效 Cookie；
- 开启请求日志；
- 用小流量验证模型映射；
- 避免长期缓存 docx/pdf 的签名下载 URL。

## 当前待验证

- Windows 本机已使用项目 `.venv` 完成启动测试；
- 已验证 `GET /v1/models`、`POST /v1/chat/completions`、`stream=true`、`POST /v1/responses`；
- 仍需要在 Linux 目标机执行 `scripts/install.sh` 做首次部署验证；
- `download.token` 的生成来源仍需继续分析；
- `ai_directans + interaction_type=20` 是否稳定避免工作区/Canvas 仍需更多样本；
- DeepSeek-V4 Flash / DeepSeek-R1 的实际 `modelName` 待抓包补充。
- 图片/文档本地代理缓存和 S3/MinIO 存储尚未实现；
- 多凭证自动轮询、失败熔断和并发调度仍需完善。

更完整的完成状态见 [PROJECT_STATUS.md](./PROJECT_STATUS.md)。
