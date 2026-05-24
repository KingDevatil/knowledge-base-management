# 代码审查检查清单 — 按模块分类

> 配合 `code-review-standards.md` 使用。每个 PR 选择对应模块的检查清单逐项勾选。

---

## 模块一：认证相关 (auth.py / admin_auth.py)

- [ ] **A1-Open Redirect**: `next` 参数是否校验了相对路径？（`routes_api.py` login 函数）
   ```bash
   grep -n "RedirectResponse.*next" src/admin/routes_api.py
   ```
- [ ] **A2-API端点头部认证**: 新路由是否调用了 `verify_api_key()`？
   ```bash
   grep -n "verify_api_key\|get_current_user" src/main.py
   ```
- [ ] **A4-API Key scope 格式**: Redis 中 scope 是 JSON 数组 `["read","write"]` 而非逗号字符串？
   ```bash
   grep -n "hset.*scope" src/auth.py
   ```
- [ ] **D1-登录限流**: 是否有基于 Redis 的登录速率限制？
   ```bash
   grep -n "rate_limit\|LoginAttempt" src/admin/routes_api.py
   ```
- [ ] **D3-Cookie 安全属性**: Session Cookie 是否设置了 `HttpOnly=True, Secure=True, SameSite=Lax`？
   ```bash
   grep -n "set_cookie" src/admin/routes_api.py
   ```

## 模块二：配置与部署 (config.py / docker-compose.yml)

- [ ] **C1-密钥硬编码**: `SESSION_SECRET`, `API_KEY_PEPPER` 默认值是否安全？
   ```bash
   grep -n "change-me\|changeme\|secret-key\|default.*secret" src/config.py
   ```
- [ ] **C2-环境变量注入**: `docker-compose.yml` 中敏感变量是否用 `${VAR:?required}`？
   ```bash
   grep -n "SECRET\|PEPPER\|PASSWORD" docker-compose.yml
   ```
- [ ] **D2-CORS 配置**: `CORS_ORIGINS` 是否为 `"*"`？生产环境应限定具体域名
   ```bash
   grep -n "CORS_ORIGINS" src/config.py
   ```

## 模块三：API 端点 (main.py / admin/routes_api.py)

- [ ] **B1-Pydantic 验证**: 所有 POST 端点是否使用 Pydantic 模型而非裸 `request.json()`？
   ```bash
   grep -n "await request.json()" src/main.py src/admin/routes_api.py
   ```
- [ ] **B2-文档大小限制**: `add_document` 是否有 `content` 长度上限？
   ```bash
   grep -n "content.*len\|max.*content\|content_size" src/tools.py
   ```
- [ ] **B3-文件上传过滤**: 上传端点是否限制了文件类型？
   ```bash
   grep -n "filename\|UploadFile\|file.*type" src/admin/routes_api.py
   ```
- [ ] **E1-外部调用错误处理**: MinIO/Redis/Chroma 调用是否有 try/except？
- [ ] **E3-HTTP 状态码**: 401（未认证）vs 403（无权限）vs 500（服务端错误）是否使用正确？
- [ ] **A3-模板权限控制**: HTML 模板中管理员按钮是否包裹了角色检查？

## 模块四：知识库核心 (knowledge_base.py / tools.py)

- [ ] **F1-写锁保护**: 所有 Chroma 写操作是否在 `async with self.write_lock` 内？
   ```bash
   grep -n "add_document_chunks\|delete_chunks\|collection_modify" src/knowledge_base.py
   ```
- [ ] **K1-锁外操作**: embedding 生成、文件读取是否在锁**外**执行？
- [ ] **G1-空输入**: `add_document("")` 如何处理？是否返回友好错误？
- [ ] **G2-超大文档**: Markdown 超过 X MB 时是否有明确限制？
- [ ] **J1/J2-类型标注**: 公共函数是否有参数和返回值类型标注？
   ```bash
   # 缺少 -> 返回类型的函数
   grep -n "async def.*):" src/knowledge_base.py | grep -v "->"
   ```

## 模块五：存储层 (source_store.py / local_store.py)

- [ ] **K5-超时设置**: MinIO 客户端是否有连接/读取超时？
   ```bash
   grep -n "Minio(\|timeout\|connect_timeout" src/source_store.py
   ```
- [ ] **E1-异常处理**: `get_object`, `put_object` 失败时是否有合理的错误处理？
- [ ] **G4-编码**: 文件读取是否统一使用 UTF-8？异常编码如何处理？

## 模块六：模板层 (admin/templates/*.html)

- [ ] **A3-权限按钮**: 管理员操作按钮（上传/新建/编辑/删除/重索引）是否在 `{% if admin.role %}` 内？
   ```bash
   grep -n "hx-post\|hx-delete" src/admin/templates/*.html
   ```
- [ ] **B4-XSS 防护**: 用户输入渲染时是否使用了 `| safe`？有无必要？
   ```bash
   grep -rn "| safe" src/admin/templates/
   ```
- [ ] **I2-模板逻辑**: 模板中是否有超过 3 行的复杂 Jinja2 逻辑？（应移到 Python 层）

---

## 审查步骤标准化

### Step 1: 自动检查（工具辅助）
```bash
# 在 mcp-gateway/ 目录下运行
cd mcp-gateway

# 1. 类型检查
mypy src/ --strict 2>&1 | head -30

# 2. 代码风格
ruff check src/ 2>&1 | head -30

# 3. 安全扫描
bandit -r src/ -ll 2>&1 | head -30

# 4. 运行已有测试
pytest tests/ -v 2>&1
```

### Step 2: 手动检查（按模块勾选）
1. 确定本次变更涉及的模块（参考上方模块分类）
2. 勾选对应模块的检查项
3. 每个 P0 问题必须修复
4. 每个 P1 问题需在 PR 描述中说明修复计划或豁免理由

### Step 3: 输出审查报告
使用标准模板（见 `code-review-standards.md` 第八节），按 P0 → P1 → P2 分类列出问题。

---

## 当前模块代码质量热力图

基于 2026-05-24 审计后修复结果：

```
auth.py          ████████░░  8/10   (API Key 认证成熟)
admin_auth.py    ███████░░░  7/10   (Session 认证可靠)
config.py        ████████░░  8/10   (↑ 移除硬编码密钥，生产环境强制校验)
main.py          ████████░░  8/10   (↑ Pydantic 验证 + CSRF 防护)
knowledge_base.py ████████░░  8/10   (Redis 缓存+写锁模式成熟)
tools.py         ████████░░  8/10   (锁内外分离优秀)
source_store.py  ████████░░  8/10   (↑ 已添加 httpx 超时)
chunker.py       ████████░░  8/10   (标题感知切片好，有单元测试)
routes_api.py    ████████░░  8/10   (↑ Open Redirect + 登录限流已修复)
templates/       ██████░░░░  6/10   (设计美观，权限控制已修复)
lock.py          █████████░  9/10   (Lua 原子释放+TTL 防死锁)
directory_store  ██████░░░░  6/10   (JSON 存储功能正常，缺并发保护)
tests/           ██░░░░░░░░  2/10   (仅 chunker 有测试，需大量补充)
```
