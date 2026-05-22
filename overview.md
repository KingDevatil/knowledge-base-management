# 企业中央知识库 + MCP Gateway — 项目概览

## 项目状态

**已完成** — 核心功能全部实现，代码通过语法检查，文档完整，可直接部署。

## 交付物清单

| 文件 | 说明 |
|------|------|
| `docker-compose.yml` | 完整编排：Redis + Chroma + MinIO + Ollama + MCP Gateway |
| `.env.example` | 环境变量模板，含所有可配置项 |
| `Makefile` | 运维命令：up/down/restart/build/logs/health/metrics/backup/clean/test/pull-model |
| `nginx/nginx.conf` | 生产环境 Nginx 配置（HTTPS + SSE + 限流 + 安全头） |
| `README.md` | 完整项目文档：架构、快速开始、MCP 配置、FAQ |
| `员工接入指南.md` | 面向员工的接入文档：Cursor/Claude/Kimi Code 配置步骤 |
| `mcp-gateway/Dockerfile` | 生产级镜像构建 |
| `mcp-gateway/.dockerignore` | 排除无需打包的文件 |
| `mcp-gateway/requirements.txt` | Python 依赖 |

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Gateway 入口 | `src/main.py` | FastAPI 应用、lifespan、路由、中间件、健康检查、指标 |
| MCP 服务器 | `src/server.py` | MCP SDK 服务器，工具注册 |
| 工具实现 | `src/tools.py` | 7 个 MCP 工具：search/add/update/delete/list/import/list_directories |
| 知识库引擎 | `src/knowledge_base.py` | Chroma 向量检索、文档 CRUD、切片管理 |
| 源文件存储 | `src/source_store.py` | MinIO 对象存储，保存原始 Markdown |
| Embedding | `src/embedding.py` | Ollama + bge-m3 文本向量化 |
| 切片 | `src/chunker.py` | Markdown 语义切片 |
| API Key 认证 | `src/auth.py` | Key 生成、验证、权限、过期、限流、吊销 |
| 管理员认证 | `src/admin_auth.py` | bcrypt + JWT Session |
| 分布式锁 | `src/lock.py` | Redis SET NX + Lua 释放 |
| 日志 | `src/logger.py` | 结构化文本日志 |
| 后台路由 | `src/admin/routes.py` | 登录/仪表盘/文档管理/API Key 管理/系统设置 |
| 后台模板 | `src/admin/templates/` | 10 个 Jinja2 模板（Jinja2 + HTMX + TailwindCSS CDN） |

## 关键特性

- **写入排队锁**：Redis 分布式锁保护 Chroma 并发写入，Embedding 生成在锁外完成
- **双层认证**：API Key（Agent 调用）+ 管理员 Session（Web 后台）
- **API Key 生命周期**：支持 1/3/7/30 天或长期有效期，过期自动作废，可手动吊销
- **源文件与切片分离**：MinIO 保存原始 Markdown，Chroma 存储向量切片
- **零构建后台**：Jinja2 + HTMX + TailwindCSS CDN，无需 npm/webpack
- **可观测性**：/health（服务健康）+ /metrics（运行指标）+ 请求日志中间件
- **CORS 支持**：可配置跨域来源，适配前端独立部署场景
- **启动检查**：lifespan 中检查所有依赖服务就绪状态

## 快速验证

```bash
# 1. 配置环境
cp .env.example .env

# 2. 启动
make up

# 3. 健康检查
make health

# 4. 查看指标
make metrics

# 5. 拉取模型
make pull-model
```

## 端口映射

| 服务 | 端口 | 说明 |
|------|------|------|
| MCP Gateway | 8000 | FastAPI 主服务 |
| Chroma | 8001 | 向量数据库 |
| MinIO | 9000 / 9001 | 对象存储 / 管理控制台 |
| Ollama | 11434 | Embedding 服务 |
| Redis | 6379 | 缓存/锁/限流 |

## 管理端点

| 端点 | 用途 |
|------|------|
| `/admin` | 后台管理登录入口 |
| `/health` | 服务健康检查（含各依赖状态） |
| `/metrics` | 运行指标（uptime、文档数、Key 数） |
| `/sse` | MCP SSE 连接端点 |
| `/api/*` | REST API（供 Agent 直接调用） |

## 后续可选优化

- [ ] 接入 Prometheus + Grafana 做完整监控告警
- [ ] 添加文档版本历史（MinIO 版本控制）
- [ ] 支持更多文件格式导入（.txt, .docx 等）
- [ ] 全文检索补强（RedisSearch / Meilisearch）
- [ ] 多语言 Embedding 模型切换

## 部署指南

### 网络架构

```
[外网用户] ──HTTPS──▶ [Nginx 443] ──proxy──▶ [mcp-gateway:8000]
                                                  │
[内网用户] ──HTTP───▶ [Nginx 80]  ──proxy──▶ [mcp-gateway:8000]
                                                  │
                                          ┌───────┼────────┐
                                          ▼       ▼        ▼
                                       Redis   Chroma   MinIO
```

### 部署方式

#### 方式一：外网 + 内网双模式（推荐）

```bash
# 1. 配置域名（编辑 .env 或设置环境变量）
export EXTERNAL_DOMAIN=wiki.yourcompany.com
export INTERNAL_DOMAIN=wiki.internal.company.com

# 2. 将 SSL 证书放入 nginx/ssl/{your-domain}/ 目录
#    nginx/ssl/wiki.yourcompany.com/fullchain.pem
#    nginx/ssl/wiki.yourcompany.com/privkey.pem

# 3. 启动（nginx 自动用 envsubst 替换模板变量）
docker compose up -d
```

- **外网访问**：`https://wiki.yourcompany.com`（HTTPS）
- **内网访问**：`http://wiki.internal.company.com` 或 `http://服务器IP`

#### 方式二：纯内网部署（免 SSL）

```bash
export EXTERNAL_DOMAIN=
export CORS_ORIGINS=http://192.168.1.100
docker compose up -d
```

内网用户通过 `http://服务器IP` 直接访问管理员页面。

## 配置说明

完整配置项参考 `README.md` 的《配置指南》章节，涵盖：

- **7 大类环境变量**：基础 / 域名网络 / CORS / 后端服务 / MinIO / 认证 / 切片参数
- **3 种部署场景**：外网+内网双模式 / 纯内网 / Windows 开发环境
- **SSL 证书配置**：Let's Encrypt 和自签名证书两种方式
- **每项变量**均标注默认值、是否必填和详细说明

### 关键配置项

| 环境变量 | 用途 | 默认值 |
|----------|------|--------|
| `EXTERNAL_DOMAIN` | 外网域名 | `kb.company.com` |
| `INTERNAL_DOMAIN` | 内网域名/IP | `kb.internal.company.com` |
| `CORS_ORIGINS` | 跨域白名单 | `*`（部署时建议指定） |
| `SESSION_SECRET` | Session 密钥 | **必须设置** |
