---
name: knowledge-base
description: >-
  使用本项目的 knowledge-base MCP 进行知识库检索、文档阅读、文档写入和关联图谱维护。
  当用户询问项目资料、内部规范、部署流程、代码/业务文档，或要求把文档保存到知识库时，优先使用本 Skill；
  即使用户没有明确说“知识库”或“MCP”，只要问题可能由内部文档回答，也应先按本 Skill 检索并引用证据。
compatibility: 需要 Agent 已连接本项目的 knowledge-base MCP，并按 API Key scope 获得 read 或 write 权限。
---

# Knowledge Base MCP 使用规范

本 Skill 指导 Agent 通过 `knowledge-base` MCP 获取内部文档证据、维护文档元数据，并使用知识图谱发现相关资料。知识库只返回文档证据，不负责替 Agent 生成最终答案；回答时应以返回的正文片段、上下文和引用为依据。

## 1. 总体原则

1. 涉及项目内部事实时，先检索，再回答；不要凭记忆补全未检索到的配置、版本、流程或数字。
2. 统一使用 `search_knowledge` 作为首选检索入口。Agent 不需要直接选择“向量检索”或手动遍历 Chroma/Redis；该工具会并发执行向量、关键词、标题/路径/核心实体结构检索，并在图谱已构建时补充关联文档。
3. 先获取小而准确的证据，再按需调用 `get_document` 阅读全文，避免把大量无关正文塞入上下文。
4. 图谱结果表示“可能有关联”，不是事实证明。最终回答必须引用实际文档片段。
5. 只读问题使用 `read` 工具；只有用户明确要求新增、修改、删除或维护时，才使用 `write` 工具。

## 2. MCP 工具清单

### 2.1 只读工具（需要 `read` scope）

| 工具 | 作用 | 典型参数/说明 |
|---|---|---|
| `search_knowledge` | 混合检索并返回切片、相邻上下文、标签、核心实体、引用 | 必填 `query`；可选 `top_k`、`filter_tags`、`filter_path`、`include_context`、`max_context_chars` |
| `get_document` | 获取单篇文档的完整源内容、元数据和全部切片 | 必填 `doc_id` |
| `list_documents` | 分页列出文档，支持目录和标签筛选 | 可选 `path`、`tags`、`limit`、`offset` |
| `list_directories` | 查看知识库目录树 | 无参数 |
| `list_document_versions` | 查看文档历史版本的摘要 | 必填 `doc_id`；不直接返回快照正文 |
| `find_similar_documents` | 写入前查找同标题、同内容或近似文档 | 必填 `title`、`content`；可选 `path`、`top_k` |

### 2.2 写入与维护工具（需要 `write` scope）

| 工具 | 作用 | 典型参数/说明 |
|---|---|---|
| `add_document` | 新增文档、切片、生成向量并写入索引 | 必填 `title`、`content`；可选 `path`、`tags` |
| `update_document` | 覆盖更新已有文档并保存版本 | 必填 `doc_id`、`title`、完整 `content`；可选 `path`、`tags` |
| `upsert_document` | 按条件创建或更新，避免重复文档 | `match_strategy` 可为 `title_path`、`hash`、`semantic`；`on_conflict` 可为 `update`、`skip`、`create_new` |
| `delete_document` | 删除源文件、切片和文档索引 | 必填 `doc_id` |
| `rename_directory` | 重命名目录并移动其子目录文档 | 必填 `old_path`、`new_path` |
| `delete_directory` | 删除目录并把其中的文档移到根目录 | 必填 `path` |
| `reindex_document` | 使用当前切片和 Embedding 配置重建单篇文档 | 必填 `doc_id`；会重新解析头部标签和实体，但保留后台独立校正值 |
| `restore_document_version` | 恢复指定历史版本 | 必填 `doc_id`、`version_id` |
| `build_knowledge_graph` | 重建实体、标签、目录和可选语义关系图及检索索引 | 可选 `semantic_threshold`，推荐从 `0.72` 起步 |

不要调用清单之外的“向量搜索”“实体提取”工具；当前实体提取是确定性规则，不依赖 LLM。

## 3. 推荐检索流程

### 3.1 普通问题

先使用完整、具体的自然语言问题：

```text
search_knowledge(
  query="生产环境 Redis 主从切换的步骤、前置条件和回滚注意事项",
  top_k=5
)
```

检索结果中重点使用：

- `content`：命中的切片正文。
- `context_before` / `context_after`：同一文档的相邻切片。
- `title`、`path`、`doc_id`、`chunk_index`：来源定位。
- `citation`：回答中的引用标识。
- `channel`：`vector`、`keyword`、`structure`、`graph` 或邻接扩展通道。
- `tags`、`entities`：文档的有效标签和核心实体（头部提取值或后台人工校正值）。
- `association_reason`：图谱结果的关联原因；只能作为发现线索。

片段足够时直接根据片段回答；只有需要全文、多个章节或完整上下文时，才调用：

```text
get_document(doc_id="检索结果中的 doc_id")
```

### 3.2 不清楚知识库结构时

先调用 `list_directories` 了解目录，再用 `list_documents(path=..., limit=..., offset=...)` 分页浏览。不要为了普通问答默认列出全库或逐篇读取正文。

`filter_path` 是精确目录匹配，不会自动包含子目录；需要子目录时先用 `list_directories` 找到具体路径，或分别检索。`filter_tags` 用于缩小标签范围。

### 3.3 按核心实体寻找关联资料

如果问题围绕一个系统、服务、模块或产品名，优先把实体名作为查询词：

```text
search_knowledge(query="MCP Gateway", top_k=8)
```

如果结果中出现 `channel=graph`，先读取关联文档的正文，再判断它们是否真的回答问题。图谱检索依赖已生成的 `retrieval_index.json`；图谱不存在或过期时，基础混合检索仍可用。

### 3.4 零命中或结果不完整

按以下顺序调整：

1. 将问题改写为“对象 + 动作 + 场景 + 约束”，例如从“部署”改为“Windows 原生部署 MCP Gateway 的环境变量配置和启动步骤”。
2. 使用文档标题、目录名或核心实体名再次查询。
3. 通过 `filter_path` 或 `filter_tags` 缩小范围后重试。
4. 仍无结果时，明确告诉用户知识库未找到足够证据；不要把推测写成内部事实。

## 4. 文档头部标签与核心实体规范

上传、编辑或重索引时，建议在 Markdown 开头写少量稳定的标签和实体。规则提取不调用本地模型或云端 LLM。

### 4.1 推荐格式

```markdown
# Gateway 局域网部署说明

> 标签：部署、运维、局域网
> 核心实体：MCP Gateway、Chroma、Redis

## 环境要求
...
```

也可以使用英文标签名或中英文混写：

```markdown
# Gateway deployment

Tags: deployment, operations
Core Entities: MCP Gateway, Chroma, Redis
```

### 4.2 兼容写法

- 标签名：`标签：`、`標籤:`、`标签:`、`Tag:`、`Tags:`。
- 实体名：`核心实体：`、`核心實體:`、`实体:`、`實體:`、`Core Entity:`、`Core Entities:`、`Entity:`、`Entities:`。
- 分隔符：中文/英文逗号、顿号、分号、竖线，例如 `MCP Gateway、Chroma | Redis`。
- 允许 Markdown 引用前缀 `>`、一级标题前缀和加粗字段名，例如 `**Entities**: Redis, Chroma`。

解析范围只限文档开头前 40 行，并在第一个二级标题 `##` 或代码块前停止；无序列表行会忽略，避免把 CSV 转换后的表格行误当成头部元数据。实体名称会按大小写、空格、连字符和下划线做保守归一，例如 `MCP Gateway` 与 `mcp-gateway` 会进入同一个实体节点，但展示时保留首次出现的写法。

建议标签和实体使用短词或稳定名称：标签用于筛选，核心实体用于跨文档关联。上传参数中的手工 `tags` 会与头部标签合并；`entities` 单独保存，并出现在搜索、文档列表、全文结果和图谱中。管理员也可在后台上传完成后或文档详情页校正标签和实体；校正不改写 Markdown，并优先于头部提取值。

### 4.3 旧文档迁移

新增和更新文档会自动解析头部。已有文档需要先调用 `reindex_document(doc_id=...)` 重新读取源文件并提取元数据；若该文档已有后台独立校正值，重索引仍会保留它。后台元数据校正目前没有独立 MCP 工具：Agent 应维护源文档头部，需校正时交由管理员在网页后台处理。批量完成后再调用 `build_knowledge_graph` 刷新实体节点和共享实体边。

## 5. 写入规范

写入前先确认用户确实要求保存或修改。新增文档建议先调用 `find_similar_documents`，避免把同标题或同内容文档重复写入：

```text
find_similar_documents(
  title="Gateway 部署说明",
  content="完整 Markdown 内容",
  path="运维/部署"
)
```

确认没有重复后，再调用：

```text
add_document(
  title="Gateway 部署说明",
  path="运维/部署",
  tags=["部署", "运维"],
  content="完整 Markdown 内容（头部可继续声明标签和核心实体）"
)
```

`update_document` 是覆盖式更新，必须提供完整标题和正文；如果要保留手工标签，应显式传入需要保留的 `tags`。写入成功后，系统会重新切片、生成 Embedding、更新关键词索引并使查询缓存失效；已有后台独立校正的标签和实体仍会优先用于检索与图谱。

文档批量增删改后不要逐篇自动重建昂贵图谱；按批次调用一次 `build_knowledge_graph`。图谱关系包括：

- `declares_core_entity`：文档关联某个有效核心实体。
- `co_entity`：文档共享核心实体。
- `co_tag`：文档共享标签。
- `same_directory`：文档位于同一目录。
- `semantically_similar`：可选的向量语义相似关系。

## 6. 等待、超时和错误

MCP 调用是请求—响应模式，Agent 会等待最终结果。若客户端在请求中携带 `progressToken` 且支持 MCP progress notification，长任务会显示排队、缓存检查、混合检索、生成向量、写入索引等阶段；不支持时仍是普通等待，不代表请求失败。

- 检索阶段超时会返回 `status=degraded`、`timed_out` 或 `retrieval_errors`，应使用已返回的基础结果，不要重复并发轰炸。
- 服务过载可能返回 `503` 和 `retry_after_ms`；按建议等待并带少量随机抖动重试。
- 写锁占用可能返回 `423` 和 `retry_after_ms`；等待后重试写入。
- 图谱缺失、过期或超时不会阻断向量、关键词和结构检索。

回答用户时，优先给出结论和引用；若只有图谱关联、没有正文证据，应明确说明“这是关联发现，不是文档事实”。
