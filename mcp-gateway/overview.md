# Admin UI 设计系统统一 — 完成总结

## 已完成内容

### 1. 设计系统建立（base.html）
- Brand Indigo-600 主品牌色 + Slate 灰阶配色体系
- 暗色模式完整支持（`darkMode: 'class'` + localStorage 持久化）
- 圆角体系：卡片 16px / 按钮 12px / 输入框 12px
- 玻璃质感 Header（`backdrop-filter: blur`）
- 微交互动效：sidebar hover 位移、统计卡片 hover 上浮、操作按钮行内显隐

### 2. Utility 类补充（base.html `<style>`）
为兼容子模板，补充了 14 组工具类（均支持亮/暗双模式）：
- `.btn-primary` / `.btn-secondary` / `.btn-danger` — 按钮体系
- `.card` — 卡片容器
- `.table-header` / `.table-cell` — 表格单元格
- `.form-input` / `.form-select` / `.form-label` — 表单元素
- `.badge-*`（6 色）— 状态徽章
- `.alert-*`（3 色）— 通知提示

### 3. 全部子模板更新（8 个页面）
| 页面 | 关键变更 |
|---|---|
| **login.html** | 渐变浮动装饰背景、玻璃卡片、密码显隐切换、暗色模式切换 |
| **dashboard.html** | 4 个统计卡片（hover 阴影+上浮）、系统健康 4 格状态（脉冲动画）、API 趋势图、快速操作区、最近活动列表 |
| **documents.html** | 左侧目录树（嵌套层级+激活态品牌色）、面包屑、搜索+排序工具栏、文档表格（hover 显隐操作）、分页、存储空间进度条 |
| **upload.html** | 全新拖拽上传区（圆角、hover 品牌色边框+背景、点击整区触发）、表单统一 `form-*` 类 |
| **document_edit.html** | 左右分栏编辑器、Markdown 实时预览、暗色模式完整适配（textarea / preview / 全 markdown 元素） |
| **document_view.html** | 面包屑（Lucide 图标）、元信息头部图标化、Markdown prose 暗色模式、底部元信息区域 |
| **api_keys.html** | 分段控件状态标签（圆角、暗色适配）、表格 badge 体系、空状态图标区 |
| **api_key_create.html** | 成功状态卡片重构、Key 复制区暗色优化、权限选项改为卡片式选择器 |
| **settings.html** | 配置卡片标题增加彩色图标区、分隔线暗色适配、危险操作区红色边框暗色模式（保持只读） |

### 4. Bug 修复
- **select 下拉列表暗色模式**：添加 `.dark select { color-scheme: dark; }` + 去除半透明背景，修复下拉选项列表在暗色下显示浅灰底的问题

### 5. 预览服务器（preview_server.py）
- 轻量 FastAPI 服务，纯模板渲染，无需外部依赖
- 注入模拟数据覆盖所有页面
- 端口 8080，随时可启停

### 6. 清理
- 删除了设计阶段遗留的 3 个静态预览文件（`*-preview.html`）

## 文件变更清单

**修改：**
- `mcp-gateway/src/admin/templates/base.html` — 设计系统母版 + utility 类
- `mcp-gateway/src/admin/templates/login.html`
- `mcp-gateway/src/admin/templates/dashboard.html`
- `mcp-gateway/src/admin/templates/documents.html`
- `mcp-gateway/src/admin/templates/upload.html`
- `mcp-gateway/src/admin/templates/document_edit.html`
- `mcp-gateway/src/admin/templates/document_view.html`
- `mcp-gateway/src/admin/templates/api_keys.html`
- `mcp-gateway/src/admin/templates/api_key_create.html`
- `mcp-gateway/src/admin/templates/settings.html`

**新增：**
- `mcp-gateway/preview_server.py` — 轻量预览服务

**删除：**
- `mcp-gateway/src/admin/templates/login-preview.html`
- `mcp-gateway/src/admin/templates/dashboard-preview.html`
- `mcp-gateway/src/admin/templates/documents-preview.html`

## Bug 修复（第二轮）

### preview_server.py 与 routes.py 对齐
1. **documents 变量名修复**：preview_server.py 传递的是 `docs`，但模板使用的是 `documents` —— 已统一为 `documents`
2. **dashboard 补充 `total_key_count`**：与 routes.py 保持一致
3. **api_keys 默认状态**：从 `"all"` 改为 `"active"`，与 routes.py 保持一致
4. **documents 补充 `q` / `tag` 参数**：避免模板渲染异常

### 7. 账户管理功能

#### 页面（account.html）
- 基本信息卡片：头像、用户名、角色标签
- 修改密码表单：当前密码 + 新密码 + 确认密码，带前端验证提示
- 成功/错误状态提示，使用设计系统 alert 样式

#### 交互入口（base.html）
- 侧边栏底部用户头像区域整体可点击，跳转 `/admin/account`

#### 后端（routes.py）
- `GET /admin/account` — 渲染账户管理页面
- `POST /admin/account/change-password` — 验证旧密码、两次新密码一致性、长度限制，调用 `AdminAuth.change_password()`

#### 认证层（admin_auth.py）
- 新增 `_save_accounts()` — 持久化账户 JSON
- 新增 `change_password()` — 验证旧密码后更新哈希
- 新增 `reset_password()` — 直接重置（用于命令行工具）

#### CLI 工具（reset_admin_password.py）
- 独立脚本，无需 Redis/FastAPI 依赖
- 用法：`python reset_admin_password.py <username> <new_password>`
- 直接读写 `admin_accounts.json`，支持环境变量指定配置文件路径

## 文件变更清单（第三轮）

**修改：**
- `mcp-gateway/src/admin/templates/base.html` — 用户头像区添加账户管理入口
- `mcp-gateway/src/admin/routes.py` — 新增账户管理路由和改密处理
- `mcp-gateway/src/admin_auth.py` — 添加 save/change/reset password 方法
- `mcp-gateway/preview_server.py` — 添加账户管理预览路由

**新增：**
- `mcp-gateway/src/admin/templates/account.html` — 账户管理页面
- `mcp-gateway/src/reset_admin_password.py` — SSH 密码重置 CLI 工具

## Bug 修复（第四轮）

### logout 未返回登录页
1. **routes.py**: `logout()` 中 `admin_auth.logout()` Redis 异常阻断了后续代码 —— 已用 `try-except` 包裹，确保 Cookie 始终删除并 302 跳转
2. **preview_server.py**: 补漏 `delete_cookie("session")`，避免浏览器带着旧 Cookie 被自动认证回去

### 统一项目名（central-knowledge-repository / Company KB → knowledge-base-management）

| 文件 | 修改内容 |
|---|---|
| `README.md` | `cd` 命令、metrics `app` 名称、MCP server key |
| `mcp-gateway/src/config.py` | `APP_NAME`、`CHROMA_COLLECTION` |
| `用户接入指南.md` | 配置名称、MCP server key |
| `implementation-plan.md` | `APP_NAME`、`CHROMA_COLLECTION`、备份脚本中的 Docker volume 前缀和部署路径 |
| `plan.md` | 目录结构示例名、MCP server key |

## 后续可扩展项
1. 系统设置页如需可配置，需设计持久化方案（运行时内存 / 配置文件 / Redis）
2. 上传页可扩展为支持多文件批量上传
3. 文档编辑器可集成 CodeMirror 或 Monaco 替代原生 textarea
4. 账户管理可扩展为多管理员体系（当前为单用户改密）
