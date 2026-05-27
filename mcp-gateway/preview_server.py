"""
Admin UI Preview Server — 轻量模板预览，无需外部依赖
"""
import os
import sys
from datetime import datetime, timezone, timedelta

# 把 src 加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Admin UI Preview")

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "src", "admin", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 模板
templates_dir = os.path.join(os.path.dirname(__file__), "src", "admin", "templates")
templates = Jinja2Templates(directory=templates_dir)

# ---------- 模拟数据 ----------

MOCK_ADMIN = {"username": "admin", "role": "admin"}

MOCK_SETTINGS = {
    "OLLAMA_MODEL": "bge-m3",
    "OLLAMA_URL": "http://localhost:11434",
    "CHUNK_SIZE": 512,
    "CHUNK_OVERLAP": 50,
    "WRITE_LOCK_TTL": 30,
    "RATE_LIMIT_DEFAULT": 30,
}

MOCK_KEYS = [
    {
        "key_hash": "abc123",
        "key_prefix": "kb_abc123",
        "applicant": "张三",
        "applicant_note": "前端组，需要查询技术文档",
        "scope": ["read"],
        "duration": "30d",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=25)).isoformat(),
        "use_count": 128,
        "status": "active",
    },
    {
        "key_hash": "def456",
        "key_prefix": "kb_def456",
        "applicant": "李四",
        "applicant_note": "后端组",
        "scope": ["read", "write"],
        "duration": "7d",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "use_count": 45,
        "status": "active",
    },
    {
        "key_hash": "ghi789",
        "key_prefix": "kb_ghi789",
        "applicant": "王五",
        "applicant_note": "",
        "scope": ["read"],
        "duration": "1d",
        "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "use_count": 12,
        "status": "expired",
    },
    {
        "key_hash": "jkl012",
        "key_prefix": "kb_jkl012",
        "applicant": "赵六",
        "applicant_note": "测试账号",
        "scope": ["read", "write"],
        "duration": "permanent",
        "expires_at": None,
        "use_count": 0,
        "status": "revoked",
    },
]

MOCK_DOCS = [
    {
        "doc_id": "doc-001",
        "title": "API 认证指南",
        "path": "技术/API",
        "tags": ["API", "认证"],
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "chunk_count": 5,
    },
    {
        "doc_id": "doc-002",
        "title": "部署手册 v2.0",
        "path": "运维/部署",
        "tags": ["部署", "Docker"],
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "chunk_count": 12,
    },
    {
        "doc_id": "doc-003",
        "title": "新用户使用指引",
        "path": "人事/入职",
        "tags": ["入职", "人事"],
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        "chunk_count": 3,
    },
    {
        "doc_id": "doc-004",
        "title": "数据库设计规范",
        "path": "技术/数据库",
        "tags": ["数据库", "规范"],
        "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        "chunk_count": 8,
    },
    {
        "doc_id": "doc-005",
        "title": "代码审查 checklist",
        "path": "技术/规范",
        "tags": ["代码审查", "规范"],
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        "chunk_count": 2,
    },
]

MOCK_TREE = {
    "name": "root",
    "path": "",
    "children": [
        {
            "name": "技术",
            "path": "技术",
            "children": [
                {"name": "API", "path": "技术/API", "children": []},
                {"name": "数据库", "path": "技术/数据库", "children": []},
                {"name": "规范", "path": "技术/规范", "children": []},
            ],
        },
        {
            "name": "运维",
            "path": "运维",
            "children": [
                {"name": "部署", "path": "运维/部署", "children": []},
            ],
        },
        {
            "name": "人事",
            "path": "人事",
            "children": [
                {"name": "入职", "path": "人事/入职", "children": []},
            ],
        },
    ],
}

# 注册模板过滤器

def format_datetime(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def format_relative_time(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt
        if diff.days > 30:
            return dt.strftime("%Y-%m-%d")
        elif diff.days > 0:
            return f"{diff.days}天前"
        elif diff.seconds > 3600:
            return f"{diff.seconds // 3600}小时前"
        elif diff.seconds > 60:
            return f"{diff.seconds // 60}分钟前"
        else:
            return "刚刚"
    except ValueError:
        return dt_str


templates.env.filters["datetime"] = format_datetime
templates.env.filters["relative_time"] = format_relative_time


# ---------- 路由 ----------

@app.get("/")
async def root():
    return RedirectResponse(url="/admin/dashboard")


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", next: str = "/admin/dashboard"):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "next": next,
    })


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "doc_count": 42,
        "active_key_count": 3,
        "expired_soon_count": 1,
        "total_key_count": 4,
        "breadcrumbs": [{"name": "仪表盘", "path": ""}],
    })


@app.get("/admin/documents", response_class=HTMLResponse)
async def documents(request: Request, path: str = ""):
    return templates.TemplateResponse("documents.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "documents": MOCK_DOCS,
        "tree": MOCK_TREE,
        "current_path": path,
        "breadcrumbs": [{"name": "文档管理", "path": ""}],
        "q": "",
        "tag": "",
        "page": 1,
    })


@app.get("/admin/documents/upload", response_class=HTMLResponse)
async def upload_page(request: Request, path: str = ""):
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "path": path,
        "error": "",
    })


@app.get("/admin/documents/{doc_id}/edit", response_class=HTMLResponse)
async def document_edit(request: Request, doc_id: str):
    doc = next((d for d in MOCK_DOCS if d["doc_id"] == doc_id), MOCK_DOCS[0])
    return templates.TemplateResponse("document_edit.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "doc_id": doc_id,
        "is_new": False,
        "title": doc["title"],
        "path": doc["path"],
        "tags": ", ".join(doc["tags"]),
        "content": "# " + doc["title"] + "\n\n这是文档内容的示例。\n\n## 概述\n\n本文档描述了相关技术细节。\n\n## 使用方法\n\n```python\nprint('hello world')\n```\n\n> 注意：这是一个引用块示例。\n",
        "error": "",
    })


@app.get("/admin/documents/new/edit", response_class=HTMLResponse)
async def document_new(request: Request):
    return templates.TemplateResponse("document_edit.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "doc_id": None,
        "is_new": True,
        "title": "",
        "path": "",
        "tags": "",
        "content": "",
        "error": "",
    })


@app.get("/admin/directories", response_class=HTMLResponse)
async def directories_page(request: Request):
    return templates.TemplateResponse("directories.html", {
        "request": request,
        "admin": MOCK_ADMIN,
    })


@app.get("/admin/api/directories")
async def api_directories():
    return JSONResponse({
        "name": "root",
        "path": "",
        "children": [
            {"name": "技术文档", "path": "技术文档", "children": [
                {"name": "API 参考", "path": "技术文档/API 参考", "children": []},
                {"name": "开发指南", "path": "技术文档/开发指南", "children": []},
            ]},
            {"name": "产品文档", "path": "产品文档", "children": [
                {"name": "需求文档", "path": "产品文档/需求文档", "children": []},
            ]},
            {"name": "运维手册", "path": "运维手册", "children": []},
        ],
    })


@app.get("/admin/api/documents")
async def api_documents_by_path(path: str = ""):
    return JSONResponse([])


@app.get("/admin/documents/{doc_id}", response_class=HTMLResponse)
async def document_view(request: Request, doc_id: str):
    doc = next((d for d in MOCK_DOCS if d["doc_id"] == doc_id), MOCK_DOCS[0])
    import markdown
    md = markdown.Markdown()
    html_content = md.convert("# " + doc["title"] + "\n\n这是文档内容的示例渲染。\n\n## 概述\n\n本文档描述了相关技术细节和使用方法。\n\n## 代码示例\n\n```python\ndef hello():\n    print('Hello, World!')\n```\n\n> 这是一个引用块，用于强调重要信息。\n\n## 列表\n\n- 第一项\n- 第二项\n- 第三项\n\n## 表格\n\n| 名称 | 类型 | 说明 |\n|------|------|------|\n| id | int | 主键 |\n| name | str | 名称 |\n")
    return templates.TemplateResponse("document_view.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "doc_id": doc_id,
        "title": doc["title"],
        "path": doc["path"],
        "tags": doc["tags"],
        "updated_at": doc["updated_at"],
        "chunk_count": doc["chunk_count"],
        "html_content": html_content,
        "source_path": f"docs/{doc['path']}/{doc['title']}.md",
        "breadcrumbs": [{"name": "文档管理", "path": ""}],
    })


@app.get("/admin/api-keys", response_class=HTMLResponse)
async def api_keys(request: Request, status: str = "active"):
    filtered = MOCK_KEYS if status == "all" else [k for k in MOCK_KEYS if k["status"] == status]
    return templates.TemplateResponse("api_keys.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "keys": filtered,
        "current_status": status,
        "breadcrumbs": [{"name": "API Key", "path": ""}],
    })


@app.get("/admin/api-keys/create", response_class=HTMLResponse)
async def api_key_create(request: Request, success: bool = False):
    if success:
        return templates.TemplateResponse("api_key_create.html", {
            "request": request,
            "admin": MOCK_ADMIN,
            "success": True,
            "created_key": "kb_live_abcdefghijklmnopqrstuvwxyz123456",
            "applicant": "测试用户",
            "scope": ["read"],
            "duration": "7天",
        })
    return templates.TemplateResponse("api_key_create.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "success": False,
    })


@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "settings": MOCK_SETTINGS,
        "breadcrumbs": [{"name": "系统设置", "path": ""}],
    })


@app.get("/admin/account", response_class=HTMLResponse)
async def account_page(request: Request):
    return templates.TemplateResponse("account.html", {
        "request": request,
        "admin": MOCK_ADMIN,
    })


@app.post("/admin/account/change-password")
async def change_password(
    request: Request,
    old_password: str = "",
    new_password: str = "",
    confirm_password: str = "",
):
    # 预览模式：仅做前端验证演示，返回成功提示
    if new_password != confirm_password:
        return templates.TemplateResponse("account.html", {
            "request": request,
            "admin": MOCK_ADMIN,
            "error": "两次输入的新密码不一致",
        })
    if len(new_password) < 6:
        return templates.TemplateResponse("account.html", {
            "request": request,
            "admin": MOCK_ADMIN,
            "error": "新密码长度至少 6 位",
        })
    return templates.TemplateResponse("account.html", {
        "request": request,
        "admin": MOCK_ADMIN,
        "success": True,
        "message": "密码修改成功（预览模式）",
    })


@app.get("/admin/logout")
async def logout():
    response = RedirectResponse(url="/admin/login")
    response.delete_cookie("session")
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
