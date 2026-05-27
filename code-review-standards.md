# 代码审查标准与流程 — knowledge-base-management

> **适用范围**: mcp-gateway 及 knowledge-base-management 全部代码
> **版本**: v1.1 | **制定日期**: 2026-05-24 | **最后更新**: 2026-05-26 | **维护**: CodeReviewExpert

---

## 一、审查维度与权重

| 维度 | 权重 | 核心关切 |
|------|------|---------|
| **安全性** | ★★★★★ | 认证绕过、注入攻击、敏感数据泄漏、权限泄漏 |
| **正确性** | ★★★★★ | 边界条件、错误处理、并发安全、数据一致性 |
| **可维护性** | ★★★★☆ | 代码清晰度、命名一致性、模块边界、类型标注 |
| **性能** | ★★★☆☆ | 锁粒度、缓存策略、数据库查询效率、大文件处理 |
| **测试覆盖** | ★★★☆☆ | 关键路径覆盖、边界测试、集成测试 |

## 二、严重等级定义

### P0 — 阻断（Blocking）
必须修复才能合并，包括：
- 安全漏洞（认证绕过、注入攻击、权限泄漏）
- 数据丢失/损坏风险
- 生产环境崩溃风险

### P1 — 严重（Critical）
强烈建议修复后再合并：
- 缺少错误处理导致的不稳定行为
- 明显的性能退化
- 硬编码敏感信息
- 缺少必要测试的关键路径变更

### P2 — 改进建议（Advisory）
可在后续迭代中修复：
- 代码风格改进
- 文档补充
- 非关键路径的测试缺失
- 类型标注遗漏

---

## 三、安全审查标准

### 3.1 认证与授权 (Auth)

| # | 检查项 | 严重度 | 检查方法 |
|---|--------|--------|---------|
| A1 | 所有管理后台端点的 `next`/`redirect` 参数是否有 Open Redirect 校验？ | P0 | 搜索 `RedirectResponse(url=.*next` 或 `redirect_url` 参数 |
| A2 | 新的 API 端点是否通过 `get_current_user()` 或 `get_admin_user()` 认证？ | P0 | 确认路由装饰器或函数内调用认证守卫 |
| A3 | 模板中管理员操作按钮是否有 `{% if admin.role in ('super_admin', 'admin') %}` 包裹？ | P0 | 检查 Jinja2 模板中 `hx-post`, `hx-delete`, `action=` 属性 |
| A4 | API Key scope 是否正确校验？Redis 中存储格式是否为 JSON 数组？ | P0 | 检查 `auth.py` 中 `verify_api_key()` 的 scope 比较逻辑 |
| A5 | 敏感操作（删除、重索引）是否有二次确认？ | P1 | 确认 HTMX `hx-confirm` 属性存在 |

### 3.2 输入验证与注入防护 (Input Validation)

| # | 检查项 | 严重度 | 检查方法 |
|---|--------|--------|---------|
| B1 | 所有 `/api/*` 端点是否使用 Pydantic 模型验证请求体（而非裸 `request.json()`）？ | P0 | 搜索 `await request.json()` + `body.get()` 模式 |
| B2 | 文档内容是否有大小限制？防止内存炸裂 | P0 | 检查 `content` 字段校验，建议上限 10MB |
| B3 | 文件上传是否有类型白名单？ | P1 | 检查上传路由中文件扩展名过滤 |
| B4 | Jinja2 模板输出是否默认自动转义？ | P1 | 确认 `autoescape` 设置，检查 `| safe` 过滤器的使用 |
| B5 | 用户输入是否用于 SQL/命令拼接？ | P0 | 搜索 f-string 或 `+` 拼接查询字符串 |

### 3.3 配置与密钥管理 (Secrets)

| # | 检查项 | 严重度 | 检查方法 |
|---|--------|--------|---------|
| C1 | 敏感配置是否有硬编码默认值？ | P1 | 检查 `config.py` 中 `Field(default=...)` 的 `SESSION_SECRET`, `API_KEY_PEPPER` 等 |
| C2 | 生产环境是否通过环境变量注入密钥？ | P1 | 检查 `docker-compose.yml` 中 `${VAR:?required}` 使用 |
| C3 | `.env` 文件是否在 `.gitignore` 中？ | P0 | 确认 `git ls-files` 不包含 `.env` |

### 3.4 其他安全控制 (Other)

| # | 检查项 | 严重度 | 检查方法 |
|---|--------|--------|---------|
| D1 | 登录端点是否有速率限制（防暴力破解）？ | P0 | 检查 `routes_api.py` 中 login 路由是否有 Redis 限流 |
| D2 | CORS 配置是否过于宽松？ | P1 | 检查 `config.py` 中 `CORS_ORIGINS` 是否为 `"*"` |
| D3 | Session Cookie 是否设置了 `HttpOnly`, `Secure`, `SameSite`？ | P1 | 检查 `set_cookie()` 调用参数 |

---

## 四、正确性审查标准

### 4.1 错误处理 (Error Handling)

| # | 检查项 | 严重度 |
|---|--------|--------|
| E1 | 所有外部调用（MinIO, Redis, Chroma, Ollama）是否有 try/except？ | P1 |
| E2 | 异常处理是否区分了可恢复 vs 不可恢复错误？ | P1 |
| E3 | HTTP 响应状态码是否符合语义？（401 vs 403 vs 500） | P2 |
| E4 | 失败时是否有有意义的错误消息返回给调用方？ | P2 |

### 4.2 并发与数据一致性 (Concurrency)

| # | 检查项 | 严重度 |
|---|--------|--------|
| F1 | Chroma 写入操作是否通过 `write_lock` 保护？ | P0 |
| F2 | 锁的 TTL（30s）是否足够覆盖最坏情况？ | P1 |
| F3 | Redis 操作失败时是否有降级策略？（Chroma 直读） | P1 |
| F4 | 目录存储（JSON 文件）是否有写锁保护？ | P1 |

### 4.3 边界条件 (Edge Cases)

| # | 检查项 | 严重度 |
|---|--------|--------|
| G1 | 空输入处理（空文档、空目录、空搜索词） | P1 |
| G2 | 超长输入处理（极长文档标题、超大 Markdown 文件） | P1 |
| G3 | 并发冲突处理（同时删除+索引同一文档） | P1 |
| G4 | 编码处理（UTF-8 边界、特殊字符、emoji） | P2 |

---

## 五、可维护性审查标准

### 5.1 代码清晰度

| # | 检查项 | 严重度 |
|---|--------|--------|
| H1 | 函数职责是否单一？（不超过 50 行或 3 层嵌套） | P2 |
| H2 | 变量/函数名是否清晰表达意图？（不用 `data`, `temp`, `result`） | P2 |
| H3 | 是否有硬编码数字/字符串？（应提取为常量或配置） | P1 |
| H4 | 复杂逻辑是否有注释说明"为什么"而非"做什么"？ | P2 |

### 5.2 模块边界

| # | 检查项 | 严重度 |
|---|--------|--------|
| I1 | 路由层是否混杂了业务逻辑？（应委托给 tools.py / admin layer） | P1 |
| I2 | 模板中是否有大量内联 Python 逻辑？（应移到 helper.py 过滤器） | P2 |
| I3 | 跨模块导入是否有循环依赖风险？ | P1 |

### 5.3 模板与上下文一致性

| # | 检查项 | 严重度 |
|---|--------|--------|
| N1 | **Settings 字段大小写**：模板中 `settings.xxx` 引用必须与 `config.py` 中 `Settings` 模型字段大小写完全一致。Pydantic 属性访问区分大小写，`settings.OLLAMA_MODEL` 和 `settings.ollama_model` 不等价。 | P1 |
| N2 | **模板上下文完整性**：路由 render 时传递的 context dict 必须包含模板中所有引用的变量。特别注意条件分支（`{% if %}`）中使用的变量在对应分支未渲染时的行为。 | P1 |
| N3 | **Mock 数据同步**：`preview_server.py` 的 Mock 数据（`MOCK_ADMIN`、`MOCK_SETTINGS` 等）必须与实际生产数据的**结构、字段名、类型和值格式**保持一致。常见错误：Mock 使用中文 role 值 `"管理员"` 但模板检查 `"admin"`。 | P1 |
| N4 | **Mock 变量名**：预览服务器传递给模板的变量名必须与真实路由保持一致。同一模板在预览和生产环境的 context key 必须相同。 | P1 |
| N5 | **Return type 变更**：当方法的返回类型发生变化时（如 `list[dict]` → `tuple[list[dict], int]`），必须更新所有调用方。使用 `grep` 确认无遗漏。 | P1 |
| N6 | **跨平台路径**：ZIP 文件中条目名必须使用 `/`（正斜杠），禁止使用 `os.sep`（Windows 为 `\`），否则 Linux/macOS 解压异常。 | P1 |

### 5.4 前端一致性规范（Tailwind + 动态渲染）

| # | 检查项 | 严重度 |
|---|--------|--------|
| M1 | **JS 动态渲染禁止使用 Tailwind 工具类**（`group-hover:`, `hover:`, `md:` 等）。原因：Tailwind JIT 编译器只扫描 `.html` 文件中的静态 class 字符串，无法识别 `innerHTML` / `createElement` 中的 class。必须用原生 CSS（`<style>` 块 + 显式 class 名，或 inline `style=`）替代 | P0 |
| M2 | 静态 HTML 模板中的 Tailwind class 可正常使用（`dashboard.html`, `base.html` 等已验证） | P2 |
| M3 | 所有 JS 动态渲染的 HTML 片段必须通过 `<style>` 块或 inline `style=` 控制样式 | P0 |
| M4 | 审查前端变更时，确认 `innerHTML = \`...\`` 字符串中不含 Tailwind 动态 class（`hover:`, `group-`, `focus:`, `md:`, `lg:` 等） | P0 |

**正确模式（JS 动态渲染）：**

```javascript
// ✅ <style> 块
// .dir-row:hover .dir-actions { opacity: 1; }

// ✅ inline style
`<span style="opacity:0; transition: opacity 0.15s">`

// ✅ 静态 class（Tailwind 能扫到）
`<span class="text-slate-400 flex items-center gap-1">`
```

**错误模式（禁止）：**

```javascript
// ❌ Tailwind 动态 class — JIT 无法扫描
`<span class="opacity-0 group-hover:opacity-100 transition-opacity">`
```

### 5.5 已发生的典型违例

| 日期 | 文件 | 问题 | 修复 |
|------|------|------|------|
| 2026-05-26 | `directories.html` | `item.innerHTML` 中使用 `group-hover:opacity-100` → 按钮不显示 | 改用 `.dir-row:hover .dir-actions { opacity: 1 }` 原生 CSS |

| # | 检查项 | 严重度 |
|---|--------|--------|
| J1 | 所有公共函数是否有参数类型标注？ | P1 |
| J2 | 所有公共函数是否有返回值类型标注？ | P1 |
| J3 | 是否有 `Any` 类型的滥用？ | P2 |
| J4 | Pydantic 模型字段是否有合理的默认值和描述？ | P2 |

---

## 六、性能审查标准

| # | 检查项 | 严重度 |
|---|--------|--------|
| K1 | 锁内代码是否足够轻量？（仅数据库写入，不含 embedding 生成） | P0 |
| K2 | 是否有不必要的同步 IO？（应用 `await` 而非阻塞调用） | P1 |
| K3 | Chroma 查询是否有合理的 `n_results` 上限？ | P1 |
| K4 | 大文档切片数量是否有上限？ | P2 |
| K5 | 外部 HTTP 客户端（MinIO, Ollama）是否设置了超时？ | P1 |

---

## 七、测试审查标准

| # | 检查项 | 严重度 |
|---|--------|--------|
| L1 | 新增/修改的核心逻辑是否有对应单元测试？ | P1 |
| L2 | 安全敏感代码（auth, lock）是否有测试覆盖？ | P1 |
| L3 | 测试是否覆盖了成功路径和至少一个失败路径？ | P2 |
| L4 | 测试数据是否隔离？（不依赖共享状态） | P2 |

---

## 八、审查流程

### 8.1 触发时机

| 场景 | 审查等级 | 说明 |
|------|---------|------|
| 新建 Python 模块 | **完整审查** | 全部维度 |
| 修改认证/安全相关代码 | **重点安全审查** | 第三、四章 |
| 修改 API 端点 | **安全 + 正确性** | 第三、四章 |
| 修改模板/UI | **权限 + XSS** | A3, B4 |
| 修改配置/部署 | **秘密 + 兼容性** | C1-C3 |
| 重构/优化 | **正确性 + 性能** | 第四、六章 |
| 补测试 | **测试质量** | 第七章 |

### 8.2 审查步骤

```
1. 通读变更       — 理解意图，15 分钟以内
2. 安全检查       — 对照第三章逐条检查，P0 问题必须修
3. 正确性检查     — 对照第四章，关注错误处理和边界
4. 代码质量检查   — 第五、六章，标记建议
5. 测试检查       — 第七章，看是否有遗漏的测试场景
6. 汇总反馈       — 按 P0 → P1 → P2 分类，附具体行号和修复建议
7. 归档           — 记录审查结论到工作记忆
```

### 8.3 审查输出模板

```markdown
## Code Review: [变更描述]

**审查日期**: YYYY-MM-DD
**审查人**: CodeReviewExpert
**严重度汇总**: P0: N | P1: N | P2: N

### P0 — 阻断问题

| # | 位置 | 问题 | 建议修复 |
|---|------|------|---------|
|   | `file.py:123` | ... | ... |

### P1 — 严重问题

| # | 位置 | 问题 | 建议修复 |
|---|------|------|---------|

### P2 — 改进建议

| # | 位置 | 问题 | 建议修复 |
|---|------|------|---------|

### 总体评估
[通过 / 修正后通过 / 需要重新设计]
```

---

## 九、自动化工具集成建议

| 工具 | 用途 | 优先级 |
|------|------|--------|
| **mypy** | 静态类型检查 | P1 — 已在 pyproject.toml 中配置 `--strict` |
| **ruff** | 代码格式 + 基础 lint | P1 — 替代 flake8+isort+black，更快 |
| **bandit** | Python 安全扫描 | P1 — 自动检测常见安全漏洞 |
| **pytest-cov** | 测试覆盖率报告 | P2 — 设置 60% 基线 |
| **pre-commit hooks** | 提交前自动检查 | P2 — 防止 P0 问题进入仓库 |

### 推荐的 pre-commit 配置

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.11.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.15.0
    hooks:
      - id: mypy
        args: [--strict]
        additional_dependencies: [pydantic>=2.0, fastapi>=0.135]
  - repo: https://github.com/PyCQA/bandit
    rev: 1.8.0
    hooks:
      - id: bandit
        args: [-c, pyproject.toml]
```

---

## 十、已修复的已知问题 (2026-05-24 修复记录)

基于 2026-05-24 代码库全面审计，已修复如下：

| # | 优先级 | 位置 | 问题简述 | 状态 |
|---|--------|------|---------|------|
| 1 | **P0** | `routes_api.py:login` | 登录无速率限制，存在暴力破解风险 | ✅ Redis 滑动窗口限流，5次/分，15次后锁5分钟 |
| 2 | **P0** | `routes_api.py:login` | `next` 参数 Open Redirect 漏洞 | ✅ `_validate_redirect_url()` 仅允许相对路径 |
| 3 | **P0** | `main.py:/api/documents` | API 端点无 Pydantic 请求体验证 | ✅ `AddDocumentRequest`/`UpdateDocumentRequest` 含长度限制 |
| 4 | **P1** | `config.py:SESSION_SECRET` | 硬编码默认值 | ✅ 空默认 + 生产环境强制 32 字符校验 |
| 5 | **P1** | `source_store.py` | MinIO 客户端无超时 | ✅ httpx 连接 5s / 读取 30s 超时 |
| 6 | **P1** | 全局 | 缺少 CSRF 防护 | ✅ Origin 头校验 + HX-Request 双重防护 |
| 7 | **P2** | 全局 | 测试覆盖率仅 ~5% | 📋 待后续迭代补充 |
| 8 | **P2** | `routes_api.py` | HTMX 响应片段内联 | 📋 待后续重构为模板文件 |
| 9 | **P1** | `routes_admin_misc.py` | 创建 API Key 时 `scope` 未传入模板上下文，页面始终显示"只读" | ✅ 2026-05-27 修复 |
| 10 | **P1** | `preview_server.py` | MOCK_ADMIN.role 用中文 `"管理员"`，模板检查 `"admin"` | ✅ 2026-05-27 修复 |
| 11 | **P1** | `preview_server.py` | MOCK_SETTINGS 字段名用全小写，与 Settings 模型大写字段不匹配 | ✅ 2026-05-27 修复 |
| 12 | **P1** | `preview_server.py` | 变量名 `full_key` 应为 `created_key`，与模板不一致 | ✅ 2026-05-27 修复 |
| 13 | **P2** | `tools_reader.py` | `list_documents` 返回的 `total` 为分页后长度而非匹配总数 | ✅ 2026-05-27 修复 |
| 14 | **P2** | `routes_admin_misc.py` | ZIP 备份路径用 `os.sep`（Windows `\`），跨平台兼容性差 | ✅ 2026-05-27 修复 |

> **注**: `CORS_ORIGINS` 保留为可配置项（通过环境变量/`.env`注入），生产环境应设置为具体域名而非 `*`。
> `knowledge_base.py` 经确认主要公共函数已有返回类型标注。

---

_本文档应与项目代码同步演进。每次重大架构变更后需重新审查更新。_
