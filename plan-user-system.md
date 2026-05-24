# 用户角色系统实现计划

## 一、目标

| 角色 | 权限 |
|------|------|
| **管理员** (admin / super_admin) | 全部权限 + 用户管理 |
| **普通用户** (viewer) | 只读文档管理页，仅可见授权目录，不可编辑/上传/删除 |

---

## 二、数据模型变更

### 2.1 `admin_accounts.json` 扩展

```json
{
  "admin": {
    "username": "admin",
    "password_hash": "$2b$12$...",
    "role": "super_admin",
    "authorized_paths": [],       // 空 = 全部目录可见（管理员）
    "created_at": "2026-05-21T00:00:00"
  },
  "zhangsan": {
    "username": "zhangsan",
    "password_hash": "$2b$12$...",
    "role": "viewer",
    "authorized_paths": ["项目资料"],   // 仅可见此目录下的文档
    "created_at": "2026-05-24T00:00:00"
  }
}
```

- `role`: `"super_admin"` | `"admin"` | `"viewer"`
- `authorized_paths`: 空列表 = 全部可见（管理员）；非空 = 仅可见指定目录（含子目录）

### 2.2 `config.example/admin_accounts.json` 同步更新

```json
{
  "admin": {
    "username": "admin",
    "password_hash": "",
    "role": "super_admin",
    "authorized_paths": [],
    "created_at": "2026-05-21T00:00:00"
  }
}
```

---

## 三、后端改动

### 3.1 `admin_auth.py` — 授权中间件

**新建两个依赖注入函数：**

```python
# 1. 要求管理员角色
async def require_admin(request: Request) -> dict:
    """仅 super_admin / admin 可访问"""
    user = await admin_auth.verify_session(request)
    if user["role"] not in ("super_admin", "admin"):
        raise HTTPException(403, "需要管理员权限")
    return user

# 2. 过滤文档路径（普通用户只能看授权目录）
def filter_authorized_paths(user: dict, target_path: str = "") -> list[str]:
    """返回该用户对此路径的可见子路径列表，或空列表表示拒绝"""
```

**新增用户 CRUD 方法到 `AdminAuth`：**

| 方法 | 说明 |
|------|------|
| `list_accounts()` | 列出所有账户（不含密码哈希） |
| `get_account(username)` | 获取单个账户 |
| `create_account(username, password, role, authorized_paths)` | 创建账户 |
| `update_account(username, role, authorized_paths)` | 更新账户角色和授权目录 |
| `delete_account(username)` | 删除账户（不可删除最后一个 super_admin） |
| `reset_account_password(username, new_password)` | 管理员重置用户密码 |

### 3.2 `admin/helpers.py` — 角色判断辅助

新增 `require_role` 工具函数和 `get_current_admin_or_viewer`：
- `get_current_admin_or_viewer` → 任意已登录用户可访问
- `require_admin` → 仅管理员可访问

### 3.3 `admin/routes_pages.py` — 路由权限加固

| 路由 | 当前守卫 | 变更后 |
|------|---------|--------|
| `/dashboard` | `get_current_admin` | `require_admin` |
| `/directories` | `get_current_admin` | `require_admin` |
| `/documents` | `get_current_admin` | `get_current_admin_or_viewer` + 按 authorized_paths 过滤 |
| `/documents/{id}` | `get_current_admin` | `get_current_admin_or_viewer` + 检查路径权限 |
| `/api-keys` | `get_current_admin` | `require_admin` |
| `/settings` | `get_current_admin` | `require_admin` |
| `/account` | `get_current_admin` | `get_current_admin_or_viewer`（保留，改密码） |

**文档列表过滤逻辑（`/documents`）：**
```python
user = await get_current_admin_or_viewer(request)
if user["role"] not in ("super_admin", "admin"):
    # 普通用户：只显示授权目录下的文档
    authorized = user.get("authorized_paths", [])
    if not authorized:
        docs = []  # 无授权则看不到任何文档
    else:
        docs = await kb.list_documents_by_paths(authorized, limit, offset)
else:
    docs = await kb.list_documents(path=path, limit=limit, offset=offset)
```

**文档查看路由加固（`/documents/{id}`）：**
```python
# 读取文档后，检查用户是否有权限
doc_path = meta.get("path", "")
if user["role"] not in ("super_admin", "admin"):
    authorized = user.get("authorized_paths", [])
    if not any(doc_path.startswith(p) for p in authorized):
        raise HTTPException(403, "无权访问此文档")
```

### 3.4 `admin/routes_api.py` — 写操作限制

所有文档 CRUD 路由升级守卫：

| 路由 | 旧守卫 | 新守卫 |
|------|--------|--------|
| `/documents/{id}/edit` | `get_current_admin` | `require_admin` |
| `/documents/{id}/delete` | `get_current_admin` | `require_admin` |
| `/documents/{id}/reindex` | `get_current_admin` | `require_admin` |
| `/documents/upload` | `get_current_admin` | `require_admin` |
| `/api/upload` | `get_current_admin` | `require_admin` |
| `/api/reindex-by-path` | `get_current_admin` | `require_admin` |
| `/api/directories/create` | `get_current_admin` | `require_admin` |
| `/api-keys/*` | `get_current_admin` | `require_admin` |
| **`/api/documents/{id}/share/create`** | `get_current_admin` | **`get_current_admin_or_viewer`** |
| **`/api/documents/{id}/shares`** | `get_current_admin` | **`get_current_admin_or_viewer`** |
| **`/api/documents/{id}/share/revoke`** | — | **`get_current_admin_or_viewer`**（仅可撤销自己创建的） |

> **分享权限说明：**
> - 普通用户可在授权目录下创建分享链接，`created_by` 字段记录分享人账号
> - 管理员在分享列表中可看到每个分享的创建人
> - 普通用户只能撤销自己创建的分享；管理员可撤销所有 |

读路由（`/api/documents`, `/api/directories`）保持 `get_current_admin_or_viewer` + 路径过滤。

### 3.5 `knowledge_base.py` — 多路径查询

新增方法：
```python
async def list_documents_by_paths(self, paths: list[str], limit=20, offset=0):
    """列出多个目录（含子目录前缀匹配）下的文档"""
    # 从 Redis 索引获取所有文档，按路径前缀匹配过滤
```

### 3.6 `main.py` — 路由注册（新增）

新增用户管理路由：
```python
from admin.user_routes import user_router
app.include_router(user_router, prefix="/admin")
```

---

## 四、前端改动

### 4.1 `base.html` — 侧边栏角色感知

```jinja2
{% if admin.role in ('super_admin', 'admin') %}
  <!-- 仪表盘 -->
  <!-- 目录管理 -->
  <!-- API Key -->
  <!-- 系统设置 -->
  <!-- 用户管理 (新增) -->
  <a href="/admin/users" class="sidebar-item ..." >
    <i data-lucide="users" class="w-5 h-5"></i>
    <span>用户管理</span>
  </a>
{% else %}
  <!-- 普通用户只显示：文档管理、账户 -->
{% endif %}
<!-- 文档管理（所有人可见） -->
<!-- 账户（所有人可见） -->
```

### 4.2 `documents.html` — 隐藏写操作按钮

```jinja2
{% if admin.role in ('super_admin', 'admin') %}
  <!-- 上传、新建按钮 -->
{% endif %}
```

操作列中：
```jinja2
{% if admin.role in ('super_admin', 'admin') %}
  <!-- 编辑、删除、重新切片按钮 -->
{% else %}
  <!-- 仅查看按钮 -->
{% endif %}
```

### 4.3 `document_view.html` — 按钮角色感知

| 按钮 | 管理员 | 普通用户 |
|------|:---:|:---:|
| 编辑 | ✅ | ❌ |
| **分享** | ✅ | **✅** |
| 删除 | ✅ | ❌ |
| 重新切片 | ✅ | ❌ |

> 普通用户可分享文档，分享链接关联到该用户账号（`created_by`），管理员可在分享列表中追溯。

### 4.4 `users.html` — 新建用户管理页面（管理员可见）

| 区域 | 内容 |
|------|------|
| 顶部 | 「新建用户」按钮 |
| 表格 | 用户名 / 角色 / 授权目录 / 创建时间 / 操作（编辑/重置密码/删除） |
| 编辑弹窗 | 用户名(只读)、角色下拉、授权目录多选、新密码(可选) |
| 新建弹窗 | 用户名、密码、角色、授权目录 |

### 4.5 `account.html` — 已有，普通用户可用

当前密码修改功能已存在，普通用户可见账户页面。

---

## 五、新增文件清单

| 文件 | 用途 |
|------|------|
| `admin/user_routes.py` | 用户管理路由（列表、创建、编辑、删除、重置密码） |
| `admin/templates/users.html` | 用户管理页面模板 |

修改文件清单：

| 文件 | 改动 |
|------|------|
| `admin_auth.py` | 新增 list/create/update/delete 方法 + `require_admin` 守卫 |
| `admin/helpers.py` | 新增 `require_admin`, `get_current_admin_or_viewer` |
| `admin/routes_pages.py` | 路由加角色守卫 + 路径过滤 |
| `admin/routes_api.py` | 写路由改为 require_admin + imports |
| `knowledge_base.py` | 新增 `list_documents_by_paths` |
| `main.py` | 注册 user_router |
| `templates/base.html` | 侧边栏角色感知 + 用户管理入口 |
| `templates/documents.html` | 根据角色隐藏上传/新建按钮 |
| `templates/document_view.html` | 根据角色隐藏编辑/删除/分享/重新切片 |
| `templates/account.html` | 微调标题去掉"管理员"字样 |
| `config.example/admin_accounts.json` | 新增 authorized_paths 字段模板 |

---

## 六、安全设计要点

1. **路径权限检查**：每个文档路由都验证 `authorized_paths`
2. **前端隐藏 ≠ 后端放过**：CRUD 路由反应用 `require_admin`，而非仅前端隐藏
3. **分享 token 可追溯**：`created_by` 记录分享人，管理员可查看谁分享了什么
4. **分享撤销控制**：普通用户只能撤销自己创建的分享；管理员可撤销任意分享
5. **不可删除最后的管理员**：`delete_account` 至少保留一个 super_admin
6. **密码不返回前端**：列表 API 不返回 password_hash

---

## 七、实施步骤（预计顺序）

| 步骤 | 内容 | 影响范围 |
|:---:|------|:---:|
| 1 | 更新 `admin_accounts.json` 格式 + `config.example` 模板 | 配置 |
| 2 | 扩展 `admin_auth.py`：用户 CRUD + `require_admin` | 核心 |
| 3 | 更新 `helpers.py`：新增守卫函数 | 核心 |
| 4 | 更新 `knowledge_base.py`：`list_documents_by_paths` | 数据 |
| 5 | 加固 `routes_pages.py`：角色守卫 + 路径过滤 | 路由 |
| 6 | 加固 `routes_api.py`：写操作 require_admin | 路由 |
| 7 | 新建 `user_routes.py`：用户管理路由 | 路由 |
| 8 | 新建 `users.html`：用户管理页面 | UI |
| 9 | 更新 `base.html`：侧边栏角色感知 | UI |
| 10 | 更新 `documents.html` / `document_view.html`：隐藏按钮 | UI |
| 11 | 注册路由，重启验证 | 集成 |
