# Knowledge Base Management

面向内部局域网的知识库 Demo：管理员通过 Web 后台维护 Markdown 文档，AI Agent 通过 MCP 检索知识库，并基于返回的片段、上下文和引用自行组织答案。

> 当前定位是“可运行、可验证的项目组知识库”，不是完整的多租户企业知识平台。核心目标是验证 `Agent → MCP → 混合/关联检索 → 文档证据` 这条链路。已提供域名、DDNS、HTTPS 反代入口，但公网暴露前仍应完成强密码、最小权限、证书、防火墙和审计配置。

## 项目能做什么

- 通过 MCP Streamable HTTP（`/mcp`）向 Agent 暴露检索与文档管理工具。
- 使用向量、关键词、标题/路径/声明实体结构三路召回，并可用加权知识图谱扩展关联文档。
- 返回切片内容、相邻上下文、文档路径、标签和可引用定位，便于 Agent 给出有依据的回答。
- 通过 Web 后台上传、编辑、删除、重建索引和管理目录。
- 使用 API Key 控制 Agent 的 `read` / `write` scope、有效期、限流和可访问目录。
- 支持文档版本、恢复、写入前相似文档检查、upsert、审计日志和知识图谱。
- 支持备份/恢复、Redis–Chroma–源文件一致性检查和重索引任务。
- Docker Compose 一体化部署；Windows 原生启动器可自动安装 Python、Ollama、Memurai 和 Python 依赖。

本项目不负责调用大模型生成最终回答，也不保存对话。Agent 调用 `search_knowledge` / `get_document` 获得证据后，自行完成总结、引用和推理。

## 文档导航

- [部署与容量配置指南](./部署与容量配置指南.md)：Docker、Windows、硬件档位、并发参数、域名和内网穿透。
- [用户接入指南](./用户接入指南.md)：API Key、Cursor、Claude Desktop、Kimi Code 和其他 MCP 客户端配置。
- [Agent 使用指南](./Agent%20使用指南.md)：检索、阅读、写入、维护工具及推荐调用流程。

## 当前架构

```mermaid
flowchart LR
    Agent["AI Agent<br/>Cursor / Kimi Code / 其他 MCP Client"]
    Browser["管理员浏览器"]
    Nginx["Nginx<br/>局域网 HTTP / 可选 HTTPS"]
    FastAPI["FastAPI Gateway"]
    Auth["API Key / Session<br/>scope · 路径 · 限流"]
    MCP["MCP Server<br/>15 个工具"]
    Admin["Web 管理后台"]
    Reader["KnowledgeToolsReader"]
    Writer["KnowledgeTools + Ingestion"]
    Retrieval["RetrievalPipeline<br/>向量 + 关键词 + 结构 + 图谱关联"]
    Graph[("加权知识图谱<br/>核心实体 · 标签 · 目录 · 语义关系")]
    Redis[("Redis<br/>文档索引 · 查询缓存 · 锁 · 统计")]
    Chroma[("Chroma<br/>切片 · 向量 · 元数据")]
    Ollama["Ollama / bge-m3"]
    Source[("MinIO<br/>或本地源文件")]
    Files[("kbdata<br/>版本 · 审计 · 备份 · 配置")]

    Agent --> Nginx --> FastAPI --> Auth --> MCP --> Reader --> Retrieval
    Browser --> Nginx --> FastAPI --> Admin --> Writer
    Retrieval --> Redis
    Retrieval --> Ollama --> Chroma
    Retrieval --> Graph
    Writer --> Ollama
    Writer --> Chroma
    Writer --> Source
    Writer --> Redis
    Writer --> Files
```

### MCP 检索链路

```mermaid
flowchart TD
    Q["Agent 调用 search_knowledge"] --> A["X-API-Key 认证<br/>scope / 目录 / 限流"]
    A --> C{"Redis 查询缓存命中?"}
    C -- 是 --> R["返回缓存结果"]
    C -- 否 --> N["规范化查询"]
    N --> V["向量通道<br/>Ollama 生成 query embedding<br/>Chroma cosine 检索"]
    N --> K["关键词通道<br/>内存倒排索引 · 中文二元词召回<br/>仅索引未就绪时回退扫描"]
    N --> S["结构通道<br/>doc_id / 标题 / 路径 / 核心实体"]
    V --> P["邻接切片扩展<br/>归一化 · 轻量重排 · 去重"]
    K --> P
    S --> P
    P --> G["图谱关联扩展<br/>按关系权重、跳数和实时权限过滤"]
    G --> E["补充前后文、标签、更新时间和 citation"]
    E --> W["写入 Redis 缓存"] --> R
```

默认查询缓存 TTL 为 300 秒。三条基础检索通道并发执行并各自受超时保护，随后使用强命中文档作为种子做图谱关联扩展；任一通道故障会记录在 `retrieval_errors` 中而不阻断其他结果。推荐档位下单个 Gateway 最多同时执行 12 个检索请求，其余请求最多排队 2000 毫秒；单次执行默认最多等待 10 秒，超限会返回降级结果或带 `retry_after_ms` 的繁忙错误。

服务启动时会构建完整的内存关键词倒排索引。索引就绪后，即使关键词零命中也会直接返回空结果，不再逐篇读取全库切片；只有启动期间索引尚未就绪时，关键词通道才使用切片扫描作为可用性兜底。结构通道仍会轻量遍历 Redis 中的文档摘要，以匹配 `doc_id`、标题和路径，但不会因此读取每篇正文。新增、更新、删除和单文档重索引会增量维护关键词索引，目录批量移动使用全量重建兜底，所有写操作都会使旧查询缓存失效。中文连续文本会同时生成二元词片，因此查询和文档不必按空格分词也能产生关键词交集。

图谱功能此前只负责生成 HTML/JSON 可视化，不参与搜索，所以对召回没有实质提升。现在构图会建立文档与有效核心实体的关联边、共享核心实体边、共享标签、同目录和可选语义相似度边，并生成 `kbdata/graph/retrieval_index.json`；检索链路按种子数、最大跳数、边权阈值和总权重受控扩展。图谱不存在、过期或超时会自动降级，不影响三条基础通道。文档批量变化或网页中校正标签/实体后，应在后台或通过 `build_knowledge_graph` 重新构建；建议启用语义关系时使用 `semantic_threshold=0.72` 起步，再按误召回情况调高。

### 文档头部标签与核心实体（零模型）

无需本地指令模型或云端 LLM。入库、编辑和重索引时会仅扫描 Markdown 开头前 40 行（遇到第一个二级标题 `##` 或代码块即停止），自动提取标签与实体。可使用中文或英文标签名、半角或全角冒号以及中英文逗号、顿号、分号、竖线分隔：

```markdown
# Gateway 部署说明

> 标签：部署、运维
> 核心实体：MCP Gateway、Chroma、Redis

或：

Tags: deployment, operations
Core Entities: MCP Gateway, Chroma, Redis
```

实体字段同时兼容 `核心实体：`、`实体：`、`Core Entities:`、`Core Entity:`、`Entities:`、`Entity:`；标签字段兼容 `标签：`、`Tag:`、`Tags:`。上传/API/编辑表单中手工提供的 `tags` 会与头部标签合并，核心实体单独保存。相同实体会按大小写、空格、连字符和下划线归一后建立同一图谱节点，例如 `MCP Gateway` 与 `mcp-gateway` 会关联；显示时保留首次出现的写法。

网页上传完成后，每个成功结果都会展示本次提取到的标签和实体，并可立即校正。文档详情页也提供“元数据”面板。这里的修改只更新检索/图谱元数据，不会改写 Markdown 正文；保存后会成为人工覆盖值，后续单文档重索引仍会保留。关键词/结构检索会立刻使用新值；已经生成的图谱是静态产物，需在“知识图谱”页面点击“重建图谱”后，实体和标签关联才会刷新。

## 快速开始：Docker 局域网部署

### 环境要求

- Windows 使用已启动的 Docker Desktop；Linux/macOS/WSL 使用 Docker Engine 或 Docker Desktop。
- Docker Compose v2，即 `docker compose` 命令。
- 最低 4 核 / 8 GB / SSD；推荐 8 核 / 16 GB；10–20 个用户持续并发建议 12 核以上 / 32 GB / NVMe，并使用 12 GB 以上显存的 NVIDIA GPU。
- 首次启动会下载镜像和约 1 GB 的 `bge-m3` 模型，需要能够访问对应镜像源。

### 一条命令启动

克隆仓库并进入项目目录后执行对应入口；首次运行会直接进入部署配置向导，无需手动复制或编辑 `.env`、生成密钥或拉取模型。

Windows Docker Desktop：

```powershell
.\start.ps1 up
# 也可以双击 start-docker.bat
```

Linux / macOS / WSL：

```bash
sh ./start.sh up
# 已安装 make 时也可执行 make up
```

首次向导会询问硬件档位、GPU、镜像源、访问方式、数据目录、Embedding 模型和管理员账号。访问方式以复选项展示，可同时启用“仅本机、局域网、公网、Cloudflare Tunnel”，然后只进入已勾选方式的具体配置。向导随后依次完成：

1. 从 `.env.example` 创建 `.env`，写入向导选项，并替换 `SESSION_SECRET` 和 MinIO 密码占位值。
2. 按 `minimum`、`recommended` 或 `high-performance` 档位写入并发、超时和图谱参数。
3. 检查 Docker、Compose 和 daemon，按选择自动检测或强制使用 CPU/NVIDIA GPU。
4. 按选择优先使用国内镜像或直接使用官方源；国内镜像拉取/构建失败时自动回退官方源。
5. 构建并启动依赖；等待 Ollama 健康后，由一次性初始化容器自动拉取 `OLLAMA_MODEL`。
6. 等待 Gateway 健康检查通过再报告完成；等待期间每 30 秒显示一次进度，超时会自动打印关键容器状态和日志。
7. 交互式部署可选择把 `knowbase` 注册到当前用户 PATH；自动化部署可使用 Windows 的 `-InstallCli` 或 Linux 的 `--install-cli`。

配置会持久化在 `.env`。访问方式菜单使用 `↑/↓` 移动，按 `Space` 勾选或取消，按 `Enter` 提交；Windows PowerShell 5、Windows Terminal 以及 Linux/macOS 常见终端均可使用。只有终端不支持逐键读取时才会自动回退为逗号分隔的编号输入。局域网具体配置会自动检测计算机名和有效 IPv4，并以逗号列表作为默认值；通常直接回车即可同时支持 `http://<计算机名>/mcp` 和 `http://<本机IP>/mcp`，多网卡时可删除不需要的候选地址。

以后只想修改部分选项时运行：

```powershell
# Windows
.\start.ps1 configure
# 也可以双击 scripts\init-config.bat
```

```bash
# Linux / macOS / WSL
sh ./start.sh configure
# 或 make configure
```

重配菜单可单独修改“硬件/GPU、镜像源、访问方式、数据/模型、初始管理员”。重新选择访问方式后，“具体配置”会依次进入局域网名称/IP、公网域名、Cloudflare Hostname/Token 等已选项目；未选项目不会被追问，公网域名和 Tunnel 配置会保留供以后重新启用。修改完成后重新执行 `up` 即可应用。首次向导中途退出时，已填内容会保留，下次 `up` 会继续完整配置。初始管理员配置只在账号库尚未创建账号时生效，已有管理员请在后台修改密码。`init` 保留为自动化使用的非交互初始化命令，CI 或无人值守部署可组合 `-NonInteractive` / `--non-interactive` 与原有参数。

### 常用部署命令

| 操作 | Windows | Linux / macOS / WSL |
|---|---|---|
| 启动并等待就绪 | `.\start.ps1 up` | `sh ./start.sh up` |
| 交互式重新配置 | `.\start.ps1 configure` | `sh ./start.sh configure` |
| 查看状态与健康 | `.\start.ps1 status` | `sh ./start.sh status` |
| 跟踪全部日志 | `.\start.ps1 logs` | `sh ./start.sh logs` |
| 停止服务 | `.\start.ps1 down` | `sh ./start.sh down` |
| 只初始化配置 | `.\start.ps1 init` | `sh ./start.sh init` |
| 强制 CPU | `.\start.ps1 up -Gpu cpu` | `sh ./start.sh up --cpu` |
| 强制 NVIDIA GPU | `.\start.ps1 up -Gpu gpu` | `sh ./start.sh up --gpu` |
| 最低硬件档位 | `.\start.ps1 up -Profile minimum` | `sh ./start.sh up --profile minimum` |
| 推荐档位 | `.\start.ps1 up -Profile recommended` | `sh ./start.sh up --profile recommended` |
| 高性能档位 | `.\start.ps1 up -Profile high-performance` | `sh ./start.sh up --profile high-performance` |
| 无人值守初始化 | `.\start.ps1 init -NonInteractive -Profile recommended -Gpu auto -Source mainland` | `sh ./start.sh init --non-interactive --profile recommended --source mainland` |
| 部署并注册全局命令 | `.\start.ps1 up -InstallCli` | `sh ./start.sh up --install-cli` |
| 单独注册全局命令 | `.\start.ps1 cli-install` | `sh ./start.sh cli-install` |

`status` 会同时显示 Compose 容器状态并请求 Gateway `/health`。启动阻塞时终端会持续给出等待进度；另一个终端可以运行 `logs` 查看模型下载、依赖健康检查和 Gateway 启动日志。

### 全局 `knowbase` 命令

部署向导结束时可以选择注册全局命令，也可以随时手动执行：

```powershell
# Windows：写入当前用户 PATH，无需管理员权限
.\start.ps1 cli-install
```

```bash
# Linux：安装到 ~/.local/bin，并更新 ~/.profile 与当前 Shell 配置
sh ./start.sh cli-install
# 已安装 make 时也可执行 make cli-install
```

Windows 默认把入口安装到已经位于用户 PATH 的 `%LOCALAPPDATA%\Microsoft\WindowsApps\knowbase.cmd`，因此无需等待 Windows Terminal 刷新新增 PATH；绑定配置保存在 `%LOCALAPPDATA%\KnowledgeBaseManagement`。少数缺少 `WindowsApps` 的精简系统会回退到独立目录，此时若原终端仍未识别，需要关闭所有 Windows Terminal 窗口后重新打开。Linux 默认安装到 `~/.local/bin`，PATH 更新后需要重新打开终端。注册信息保存项目绝对路径，整个项目移动后命令会明确提示失效，此时在新目录重新执行 `cli-install` 即可更新绑定。

| 命令 | 作用 |
|---|---|
| `knowbase up` / `knowbase down` / `knowbase restart` | 启动、停止或重启完整 Docker 服务 |
| `knowbase status` / `knowbase logs` | 查看完整服务状态或持续日志 |
| `knowbase configure` / `knowbase init` | 交互重配或非交互初始化配置 |
| `knowbase health [--json]` | 请求本机 Gateway `/health`，失败时返回非零退出码 |
| `knowbase gateway start\|stop\|restart` | 自动管理当前 Gateway：运行中的容器优先走 Docker；存在原生 `.env.local` 或未安装 Docker 时走 Windows 原生服务 |
| `knowbase gateway status\|logs\|health` | 查看当前 Gateway 的状态、日志和健康状态 |
| `knowbase native start\|stop\|restart\|status\|logs` | Windows 原生模式管理；Linux 使用 Docker 模式 |
| `knowbase doctor` | 检查项目目录、`.env`、Docker Compose 和 Gateway 健康 |
| `knowbase cli status\|uninstall` | 检查或卸载全局命令 |

`knowbase gateway start` 和 `restart` 会等待健康检查通过再返回，期间每 10 秒输出一次进度；默认检查 `http://127.0.0.1:8000/health`，可通过 `KNOWBASE_HEALTH_URL` 或 `knowbase health --url <地址>` 覆盖。在 Windows 上可追加 `--native` 或 `--docker` 强制选择运行方式，例如 `knowbase gateway restart --native`；未指定时，运行中的容器优先使用 Docker，存在 `.env.local` 或未安装 Docker 时使用原生服务。

### 访问地址

Docker 默认把 Gateway、Chroma、Redis、MinIO、Ollama 的宿主机端口绑定到 `127.0.0.1`，只把 Nginx 的 `80/443` 暴露给局域网。因此局域网客户端应使用：

| 用途 | 局域网地址 |
|---|---|
| 管理后台 | `http://<服务器IP>/admin` |
| MCP Streamable HTTP | `http://<服务器IP>/mcp` |
| MCP SSE 兼容端点 | `http://<服务器IP>/sse` |
| 健康检查 | `http://<服务器IP>/health` |
| 运行指标 | `http://<服务器IP>/metrics` |

`http://127.0.0.1:8000/*` 是服务器本机直连 Gateway 的地址，不是 Docker 局域网接入地址。

### 域名、DDNS 与内网穿透

- 配置向导中的四个访问方式是复选项，并非互斥模式；用方向键依次勾选“局域网、公网、Cloudflare Tunnel”即可让三种入口同时工作。
- 有公网 IP：勾选“公网”，将域名解析到服务器公网 IP，在路由器/安全组开放 80、443，并把证书放到 `nginx/ssl/<域名>/fullchain.pem` 和 `privkey.pem`。后台“设置”页也可保存 DDNS 与反向代理方案。
- 动态公网 IP：后台 DDNS 支持 Cloudflare 等 Provider，负责更新 A/AAAA 记录；仍需确认运营商未使用 CGNAT，并配置端口转发。
- 无公网 IP/CGNAT：勾选“Cloudflare Tunnel”。在 Cloudflare 控制台创建 remotely-managed tunnel，把 Public Hostname 的 Service 设为 `http://nginx:80`，再输入 Hostname 和 Token。
- 公网直连与 Tunnel 同时启用时应使用两个不同主机名，例如 `kb.example.com` 与 `tunnel.kb.example.com`。向导分别保存两者，并根据所选入口生成运行域名和 CORS 配置；后续生命周期命令会从 `.env` 复用全部访问方式。

无论哪种方式，都只应公开 Nginx/Gateway 入口，不要把 Redis、Chroma、MinIO 或 Ollama 端口暴露到公网。完整步骤见 [部署与容量配置指南](./部署与容量配置指南.md)。

### 初始化知识库和 API Key

1. 打开 `http://<服务器IP>/admin`。
2. 首次启动且账号文件为空时，系统会用 `ADMIN_INITIAL_USERNAME` / `ADMIN_INITIAL_PASSWORD` 创建 `super_admin`。
3. 在后台上传或新建 Markdown 文档；也可上传 CSV，系统会保留列名并按记录转换为适合检索的 Markdown 内容。上传完成后可直接校正提取到的标签和核心实体，校正不会改动正文。
4. 在“API Key”页面创建 Agent 使用的 Key；完整 Key 只展示一次。
5. 只检索时勾选 `read`。如需同一个 Agent 同时检索和写入，必须同时勾选 `read` 与 `write`；两个 scope 相互独立。

如果 Key 使用受限目录模式，Agent 应在搜索和列举时显式传入允许的 `filter_path` / `path`。

## Windows 原生开发模式（不使用 Docker）

日常部署优先使用上面的 Docker 入口。确实需要调试 Python 源码时，可直接运行 Windows 原生脚本：

```powershell
.\start-dev.ps1
# 或双击 start-dev.bat
```

首次运行会自动创建独立的 `.env.local`，优先通过 `winget` 安装缺失的 Python 3.13、Ollama 和 Memurai；`winget` 不可用时，Python 按清华镜像 → Python.org 回退，Memurai 使用官方 MSI，MinIO 按中国镜像 → 官方源回退。随后安装 Python 依赖、启动或复用服务，并在缺少时拉取 `bge-m3`。安装系统软件时 Windows 可能显示 UAC 确认。

```powershell
# 修改本地配置
notepad .env.local

# 强制重新检查并安装 requirements.txt
.\start-dev.ps1 -Install

# 按硬件档位启动；受管控电脑可禁止自动安装系统软件
.\start-dev.ps1 -Profile minimum
.\start-dev.ps1 -NoAutoInstall

# 只生成/校验 .env.local，不安装依赖、不创建数据目录、不启动服务
.\start-dev.ps1 -InitOnly -Profile recommended

# 后台启动；脚本会等到 Gateway /health 返回 200 后才退出
.\start-dev.ps1 -Background

# 只停止本项目占用 8000/8001/9000/9001 的 Gateway、Chroma、MinIO
.\start-dev.ps1 -Stop
```

原生启动器兼容 `start-dev.bat` 使用的 Windows PowerShell 5.1。Redis 不再以“6379 端口已监听”作为就绪条件，而会执行真实 `PING`；优先启动已安装的 Memurai Windows 服务，失败时再尝试直接运行可执行文件。Gateway 会在后台拉起并持续轮询 `/health`，等待期间每 10 秒显示进度，只有所有依赖健康且 `/health` 返回 200 才显示“全部健康”；失败时会显示 `mcp-gateway-dev.stderr.log` / `mcp-gateway-dev.stdout.log` 尾部。默认模式保持前台热重载，`-Background` 使用稳定的非热重载进程并在健康后返回终端。

`-Stop` 不会终止机器上共享的 Ollama 或 Redis/Memurai 进程，只停止本项目监听端口的 Gateway、Chroma 和 MinIO（包括 Gateway 热重载进程树）。若 8000 端口已被占用但健康检查失败，脚本会明确停止并提示先检查或执行 `-Stop`，不会继续显示“全部就绪”。`scripts\init-config.bat` 用于生成或重新配置 Docker 的 `.env`，不用于原生开发模式；需要双击停止原生服务时可使用 `scripts\stop-dev.bat`。

也可以使用桌面入口：

```powershell
.\scripts\start-desktop-shell.bat
# 旧版 tkinter 启动器
.\scripts\start-gui.bat
```

本地模式默认按 `.env.local` 的 `BIND_HOST=0.0.0.0` 监听 `http://<主机>:8000`。仅需本机访问时改为 `127.0.0.1`；允许局域网访问时还需配置 Windows 防火墙。

## 连接 MCP

### 推荐：Streamable HTTP

在支持远程 MCP 的客户端中加入：

```json
{
  "mcpServers": {
    "knowledge-base": {
      "url": "http://192.168.1.100/mcp",
      "headers": {
        "X-API-Key": "sk-your-api-key"
      }
    }
  }
}
```

本地原生运行时可改为 `http://192.168.1.100:8000/mcp`。客户端配置格式会随产品版本变化，请以客户端当前支持的远程 MCP 配置为准；关键是 URL 和 `X-API-Key` Header。

### 兼容端点：SSE

旧客户端可以连接 `/sse`，消息端点由服务端协商为 `/sse/messages/`。如果客户端只支持 stdio，可在客户端侧使用 MCP HTTP/SSE 到 stdio 的代理。优先使用 Header 传 Key，避免把 Key 放进 URL、日志和历史记录。

## Agent 推荐调用方式

一个稳妥的只读流程是：

1. 不清楚知识结构时，先调用 `list_directories` 或 `list_documents`。
2. 调用 `search_knowledge`，使用完整、具体的问题，通常先取 `top_k=5`。
3. 优先使用结果中的 `content`、`context_before`、`context_after` 和 `citation` 回答。
4. 只有确实需要全文时才调用 `get_document`，避免把长文档全部塞入 Agent 上下文。
5. 结果不足时换一种问法，或通过 `filter_path` / `filter_tags` 缩小范围再次检索。

示例参数：

```json
{
  "query": "生产环境发布失败后应该如何回滚，执行顺序和检查点是什么？",
  "top_k": 5,
  "filter_path": "运维/发布",
  "filter_tags": ["生产", "回滚"],
  "include_context": true,
  "max_context_chars": 1200
}
```

当前 `filter_path` 是精确目录匹配，不会自动包含子目录。`max_context_chars` 限制每条结果的前后相邻上下文总字符数；设 `include_context=false` 可跳过相邻切片读取，适合先低成本筛选再按 `doc_id` 精读。`score` 是多通道归一化后的相对排序分数，不应当作严格概率或事实置信度。

`search_knowledge` 的主要返回字段：

| 字段 | 含义 |
|---|---|
| `content` | 命中的切片正文 |
| `context_before` / `context_after` | 同一文档的相邻切片 |
| `context_truncated` | 相邻上下文是否因字符预算被截断 |
| `title` / `path` / `doc_id` | 来源文档定位 |
| `chunk_index` / `total_chunks` | 切片位置 |
| `citation` | 形如 `目录:标题#chunk-N` 的引用标识 |
| `channel` | `vector`、`keyword`、`structure`、`graph` 或邻接扩展通道 |
| `association_reason` | 图谱结果的种子文档、跳数、关系类型和累计边权 |
| `score` / `raw_score` / `final_score` | 排序与调试分数 |
| `tags` / `entities` / `updated_at` | 文档标签、有效核心实体（头部提取或人工校正）和更新时间 |
| `retrieval_errors` | 某一检索通道失败时的降级信息 |
| `cache_hit` | 是否命中 Redis 查询缓存 |
| `status` / `timed_out` | `ok` 或 `degraded`，以及本次是否发生阶段/总超时 |
| `timings_ms` | 检索、上下文补充和总耗时（毫秒） |

## MCP 工具

### 只读工具

| 工具 | 说明 | 主要参数 | Scope |
|---|---|---|---|
| `search_knowledge` | 混合检索并返回切片、可控上下文和引用 | `query`; 可选 `top_k`, `filter_tags`, `filter_path`, `include_context`, `max_context_chars` | `read` |
| `get_document` | 获取文档源内容、元数据和全部切片 | `doc_id` | `read` |
| `list_documents` | 按精确目录/标签分页列出文档 | 可选 `path`, `tags`, `limit`, `offset` | `read` |
| `list_directories` | 返回目录树 | 无 | `read` |
| `list_document_versions` | 列出文档版本快照（不含快照正文） | `doc_id` | `read` |
| `find_similar_documents` | 写入前检查同标题、同内容或近似文档 | `title`, `content`; 可选 `path`, `top_k` | `read` |

### 写入与维护工具

| 工具 | 说明 | 主要参数 | Scope |
|---|---|---|---|
| `add_document` | 新增 Markdown 文档并切片、向量化 | `title`, `content`; 可选 `path`, `tags` | `write` |
| `update_document` | 覆盖更新文档，写入前保存版本 | `doc_id`, `title`, `content`; 可选 `path`, `tags` | `write` |
| `upsert_document` | 按标题路径、内容哈希或近似度创建/更新 | `title`, `content`; 可选 `match_strategy`, `on_conflict` 等 | `write` |
| `delete_document` | 删除源文件、切片和文档索引 | `doc_id` | `write` |
| `rename_directory` | 重命名目录并移动子目录文档 | `old_path`, `new_path` | `write` |
| `delete_directory` | 删除目录并把其中的文档移到根目录 | `path` | `write` |
| `reindex_document` | 使用当前切片和 Embedding 配置重建单篇文档 | `doc_id` | `write` |
| `restore_document_version` | 恢复指定版本 | `doc_id`, `version_id` | `write` |
| `build_knowledge_graph` | 生成核心实体、标签、目录和可选语义关系图 | 可选 `semantic_threshold` | `write` |

MCP 工具调用仍是请求—响应模式，调用方会等待最终结果。客户端在请求中提供 `progressToken` 且支持展示 MCP progress notification 时，`search_knowledge` 会依次提示“等待检索执行槽位”“检查查询缓存”“执行向量、关键词和结构混合检索”“补充上下文”等阶段；`add_document`、`update_document` 和 `reindex_document` 也会发送“生成向量”“等待写入锁”“替换文档索引”等 0–100 阶段提示。不支持进度通知的客户端只会表现为普通等待，但最终结果不受影响。检索仍有总时限、阶段时限和并发排队上限；超时会返回 `degraded` 信息，过载会快速返回 `503` 和建议重试时间。

写入使用 Redis 分布式锁串行提交，Embedding 会尽量在进入锁之前完成，持锁期间会周期性续租。锁被占用时返回 `423`，错误详情包含 `retry_after_ms`，调用方可据此延迟重试。`update_document` 是覆盖式更新；请显式提供需要保留的标签。

## 文档入库链路

新增文档会经过以下节点：

1. 校验标题和 Markdown 内容。
2. 统一换行、规范目录和标签；从文档头部规则提取标签与核心实体（中英文兼容、无需模型）。
3. 按 Markdown 标题/段落切片。
4. 使用 Ollama 批量生成 Embedding；可按配置切换到备用 Ollama Provider。
5. 获取写锁后保存 Markdown 源文件。
6. 把切片、Embedding 和元数据写入 Chroma。
7. 把文档摘要和内容哈希写入 Redis 文档索引。
8. 增量更新该文档的关键词索引并使查询缓存失效。

更新和重索引会先写 staging 数据，提交失败时尽量恢复旧切片；更新、删除和恢复前会写入文档版本快照。

> 有标题和自然段的 Markdown 能提供更好的检索上下文。完全不换行的超长段落会按 `CHUNK_SIZE` / `CHUNK_OVERLAP` 滑动切片，不会截断尾部内容。

## 存储职责

| 存储 | 保存内容 | 是否可重建 |
|---|---|---|
| Chroma | 文档切片、Embedding、检索元数据 | 可由 Markdown 源文件重新入库/重索引 |
| MinIO | 原始 Markdown 源文件 | 主要事实来源之一，需要备份 |
| 本地文件存储 | MinIO 不可用时的源文件 Adapter | 需要备份 |
| Redis | API Key 运行态、文档摘要索引、查询缓存、统计、限流、写锁 | 文档摘要索引会在启动时与 Chroma 对账；其他部分可由配置文件恢复或作为缓存丢弃 |
| `kbdata/config` | 账号、API Key 持久化、目录、审计日志、运行配置 | 需要备份 |
| `kbdata/versions` | 文档历史快照 | 需要备份 |
| `kbdata/backups` | 后台创建的备份包和索引 | 备份输出 |
| `kbdata/graph` | 知识图谱 JSON/HTML 与关联检索索引 | 可重新生成 |

系统提供一致性检查，用于发现 Redis 文档索引、Chroma 切片和源文件之间的缺失、孤儿数据或数量不一致：

```bash
# Docker
docker compose exec mcp-gateway python src/consistency_cli.py

# 本地
cd mcp-gateway
python src/consistency_cli.py
```

## 主要配置

完整的常用示例见 `.env.example`（Docker）和 `.env.example.local`（Windows 本地）。

| 配置 | 默认/示例 | 说明 |
|---|---|---|
| `HARDWARE_PROFILE` | `recommended` | 配置向导选择的硬件档位 |
| `DEPLOY_CONFIGURED` | `true`（向导完成后） | 标记首次配置是否完整；中途退出时下次继续向导 |
| `DEPLOY_GPU_MODE` | `auto` | `auto` / `cpu` / `gpu`；由所有部署命令自动复用 |
| `DEPLOY_IMAGE_SOURCE` | `mainland` | `mainland` 国内镜像优先并自动回退，或 `official` |
| `DEPLOY_ACCESS_MODES` | `lan` | 组合访问方式，逗号分隔 `local` / `lan` / `domain` / `cloudflare` |
| `DEPLOY_ACCESS_MODE` | `lan` | 旧版本兼容字段；由启动器派生，多种方式时为 `hybrid` |
| `DEPLOY_TUNNEL_MODE` | `off` | `off` / `cloudflare`；启用后自动加载 Compose profile |
| `HOST_KBDATA_DIR` | `./kbdata` | Docker 宿主机数据目录 |
| `KBDATA_DIR` | Docker 内固定 `/app/data` | 本地运行时的数据根目录 |
| `SESSION_SECRET` | 无有效默认值 | `DEBUG=false` 时必须至少 32 字符 |
| `ADMIN_INITIAL_USERNAME` | `admin` | 账号文件为空时创建的超级管理员 |
| `ADMIN_INITIAL_PASSWORD` | `123456` | 仅初始化时使用，Demo 也建议修改 |
| `REDIS_URL` | `redis://localhost:6379/0` | Docker Compose 会覆盖为 `redis` |
| `CHROMA_HOST` / `CHROMA_PORT` | `localhost` / `8001` | Docker 内部覆盖为 `chroma:8000` |
| `CHROMA_COLLECTION` | `knowledge_base_management` | Chroma collection 名称 |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `bge-m3` | 主 Embedding Provider |
| `EMBEDDING_FALLBACKS` | 空 | `url|model,url|model` 格式的备用 Ollama Provider |
| `SEARCH_CACHE_TTL` | `300` | 查询结果缓存秒数；设为 `0` 关闭 |
| `SEARCH_TOTAL_TIMEOUT_MS` | `10000` | 单次检索请求总时限；超时返回降级结果 |
| `SEARCH_VECTOR/KEYWORD/STRUCTURE_TIMEOUT_MS` | `7000/3000/2000` | 三个基础通道的独立超时 |
| `SEARCH_ENRICH_TIMEOUT_MS` | `1500` | 标签、引用和相邻上下文补充阶段时限 |
| `SEARCH_MAX_CONCURRENCY` | `12` | 同一 Gateway 进程并发执行的检索上限 |
| `SEARCH_QUEUE_TIMEOUT_MS` | `2000` | 等待检索并发槽位的最长时间，超出返回繁忙错误 |
| `SEARCH_CONTEXT_MAX_CHARS` | `2000` | 未显式传参时，每条检索结果的相邻上下文总字符预算 |
| `GRAPH_RETRIEVAL_ENABLED` | `true` | 是否让已构建图谱参与关联检索 |
| `GRAPH_RETRIEVAL_WEIGHT` | `0.35` | 图谱候选相对权重，过高可能增加关联但不直接回答问题的结果 |
| `GRAPH_RETRIEVAL_MAX_RESULTS/MAX_HOPS` | `3/2` | 每次最多扩展文档数和关系跳数 |
| `GRAPH_RETRIEVAL_MIN_EDGE_WEIGHT` | `0.25` | 忽略低于该值的弱关系 |
| `EMBEDDING_MAX_CONNECTIONS` | `24` | Gateway 到 Ollama 的 HTTP 连接上限 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `512` / `50` | Markdown 切片参数，单位为字符 |
| `WRITE_LOCK_TTL` | `30` | Redis 写锁过期秒数 |
| `RATE_LIMIT_DEFAULT` | `30` | API Key 每分钟默认 HTTP 请求数 |
| `MINIO_ENDPOINT` / `MINIO_BUCKET` | `localhost:9000` / `kb-sources` | 源文件对象存储 |
| `PUBLIC_DOMAIN` | 可为空 | 公网直连的 HTTPS 域名，未勾选公网时保留但不启用 |
| `CLOUDFLARE_PUBLIC_HOSTNAME` | 可为空 | Cloudflare Tunnel 的 Public Hostname，可与公网直连同时保存 |
| `EXTERNAL_DOMAIN` | 可为空 | 向导派生的当前运行域名；兼容 Nginx 和旧配置，不建议手工作为组合模式来源 |
| `INTERNAL_DOMAIN` | `主机名,192.168.1.100` | Nginx 内网 server name 列表；逗号分隔，可由向导自动检测 |
| `CORS_ORIGINS` | `*` | Demo 可用；对外部署时应收紧 |

Embedding Provider 还支持健康检查缓存、失败阈值和熔断冷却：`EMBEDDING_HEALTH_CACHE_TTL`、`EMBEDDING_FAILURE_THRESHOLD`、`EMBEDDING_CIRCUIT_COOLDOWN`。三个硬件档位的完整参数与调优方法见 [部署与容量配置指南](./部署与容量配置指南.md)。

## Web 后台

后台当前包含：

- 文档列表、全文查看、Markdown 编辑、下载、Markdown/CSV 批量上传和压缩包导入；上传结果和文档详情均可独立编辑标签/核心实体，不改动正文。CSV 会按记录保留列名后入库，便于 MCP 检索表格数据。
- 目录、标签、用户角色和路径权限管理。
- API Key 创建、吊销、删除和使用统计。
- 文档版本与恢复、入库/清理/重索引任务。
- 知识图谱生成与查看。
- 备份策略、备份创建、下载、恢复和一致性维护。
- 审计日志、运行健康、DDNS、反向代理和环境配置页面。

Web 角色为 `super_admin`、`admin`、`user`（可编辑授权目录）和 `viewer`（只读授权目录）。API Key scope 与 Web 角色是两套独立机制。

## 备份与恢复

推荐通过后台维护页面创建和恢复备份。也可以导出 Markdown 源文件：

```bash
docker compose exec mcp-gateway python src/backup_sources.py -o /app/data/backups
```

最重要的数据是 Markdown 源文件、`kbdata/config` 和 `kbdata/versions`。只备份 Chroma 不能完整恢复账号、API Key、源文档和版本历史。

## 测试

```powershell
python -m pip install -r mcp-gateway/requirements-dev.txt
python -m pytest -q
python scripts/check_deps_sync.py
```

Linux / CI：

```bash
python -m pip install -r mcp-gateway/requirements-dev.txt
python -m pytest -q
python scripts/check_deps_sync.py
```

根目录的 `pytest.ini` 已将标准测试限定到 `mcp-gateway/tests`；`scripts/test.ps1` / `scripts/test.sh` 是等价的跨平台包装。`tests/test_launcher.py` 和 `tests/process_test.py` 是独立诊断脚本，后者可能启动或终止本机 Gateway 进程，请按需单独执行。

## 目录结构

```text
knowledge-base-management/
├── docker-compose.yml              # Nginx、Gateway、Redis、Chroma、MinIO、Ollama
├── docker-compose.gpu.yml          # NVIDIA GPU 覆盖配置
├── docker-compose.official.yml     # 国内镜像失败后的官方源覆盖
├── deploy/profiles/                # minimum/recommended/high-performance 档位
├── .env.example                    # Docker 配置模板
├── .env.example.local              # Windows 本地配置模板
├── start.sh / start.ps1            # Linux/Windows Docker 统一部署入口
├── start-docker.bat / Makefile     # Docker 双击入口与命令包装
├── start-dev.ps1 / start-dev.bat   # Windows 原生开发一键入口
├── 部署与容量配置指南.md             # 镜像、并发、硬件、域名与穿透配置
├── mcp-gateway/
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── src/
│   │   ├── main.py                 # FastAPI 组合根、lifespan、MCP transport
│   │   ├── server.py               # MCP 工具 schema、scope/path guard、dispatch
│   │   ├── tools_reader.py         # 只读工具、查询缓存、结果增强
│   │   ├── tools.py                # 写工具、版本、upsert、staging/恢复
│   │   ├── rag/
│   │   │   ├── retrieval.py        # 三路召回、邻接扩展、归一化与重排
│   │   │   └── keyword_index.py    # 内存关键词倒排索引
│   │   ├── ingestion.py            # 文档入库节点 Pipeline
│   │   ├── knowledge_base.py       # Chroma + Redis 文档索引 Adapter
│   │   ├── embedding.py            # Ollama Provider、fallback 与熔断
│   │   ├── chunker.py              # Markdown 标题/段落切片
│   │   ├── source_store.py         # MinIO 源文件 Adapter
│   │   ├── local_store.py          # 本地源文件 Adapter
│   │   ├── auth.py                 # Agent API Key
│   │   ├── admin_auth.py           # Web Session 与账号
│   │   ├── backup_manager.py       # 备份、恢复、计划任务
│   │   ├── consistency.py          # 三存储一致性检查
│   │   └── admin/                   # 后台路由与 Jinja2 模板
│   └── tests/                       # Gateway 单元/集成风格测试
├── kbdata/                          # 默认运行时数据根目录
├── nginx/                           # 动态 Nginx 配置生成
├── scripts/                         # 辅助启动、测试与维护脚本
│   ├── knowbase.ps1 / knowbase.sh  # Windows/Linux CLI 命令路由
│   ├── install-cli.ps1 / .sh       # 用户级 PATH 安装、检查和卸载
│   ├── network-detection.ps1 / .sh # 自动检测计算机名和局域网 IPv4
│   ├── access-modes.ps1 / .sh      # 键盘复选菜单、访问方式迁移和组合开关
│   ├── init-config.bat             # Docker 交互配置向导
│   ├── stop-dev.bat                # 停止 Windows 原生服务
│   ├── start-desktop-shell.bat     # Windows 桌面壳入口
│   ├── start-gui.bat               # 旧版 tkinter 启动器
│   ├── test.ps1 / test.sh          # 跨平台测试入口
│   └── check_deps_sync.py          # 依赖声明一致性检查
├── 用户接入指南.md
└── Agent 使用指南.md
```

根目录只保留用户最常用的一键部署入口；其余辅助入口集中在 `scripts/`。这些脚本都根据自身位置解析项目根目录，不依赖启动命令所在的当前工作目录，因此可从资源管理器双击或从任意目录调用。脚本用途和调用方式见 [`scripts/README.md`](scripts/README.md)。

## 当前阶段限制

- 以 Markdown 文档和单一共享知识库为主，没有多租户知识空间。
- 检索是本地混合召回和启发式重排，没有 Cross-Encoder 或 LLM reranker。
- 路径筛选为精确目录；递归目录搜索需要 Agent 分目录调用或后续扩展。
- 写入提交被全局锁串行化，适合 Demo 的低写入量，不适合高并发采集。
- `get_document` 返回全文和全部切片，超长文档可能占用较多 Agent 上下文。
- Redis 是 MCP 认证、限流、文档摘要索引和缓存的关键依赖。
- 本阶段面向可信局域网。若要公网或生产使用，应另行完成安全、观测、容量和灾备评审。

## 技术版本

| 模块 | 当前版本/镜像 |
|---|---|
| Python | 3.11+；CI 使用 3.13 |
| FastAPI / Starlette | 0.135.3 / 1.0.0 |
| MCP Python SDK | 1.16.0 |
| Chroma | 1.5.9 |
| Ollama | 0.6.0，默认模型 `bge-m3` |
| Redis | 7-alpine |
| MinIO | `RELEASE.2025-01-20T14-49-07Z` |
| Nginx | 1.27-alpine |

## License

当前仓库未包含独立的 `LICENSE` 文件；如需对外分发，请先补充明确的许可声明。
