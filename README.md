<div align="center">

# kb-mcp-lite
**面向AI代理的轻量级本地知识库 · 团队协作友好**

`pip install kb-mcp-lite` — 让任何AI编程助手都拥有结构化、可查询、可同步的团队"第二大脑"

[![PyPI version](https://img.shields.io/badge/pypi-v0.8.0-blue)](https://pypi.org/project/kb-mcp-lite/)
[![Python](https://img.shields.io/badge/python-≥3.10-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![MCP](https://img.shields.io/badge/MCP-兼容-purple)](https://modelcontextprotocol.io/)
[![Status: 测试版](https://img.shields.io/badge/status-beta-green)](#状态说明)

</div>

---

## 🎯 解决什么问题
当前的知识管理工具存在明显的断层：
- 面向人类的知识库（Notion/Obsidian）：需要手动维护，AI无法直接高效访问
- 向量数据库：需要复杂配置，没有结构化Schema，团队协作困难
- 团队文档散落在各个地方：代码注释、Wiki、PR描述、飞书文档，AI找不到也用不了

`kb-mcp-lite` 专门填补这个空白：**专为AI代理设计，同时兼顾人类编辑和团队协作**，让AI编程助手可以直接调用团队沉淀的所有技术知识。

| 对比维度 | Notion/Obsidian | Chroma/LanceDB等向量库 | **kb-mcp-lite** |
|---|---|---|---|
| 服务对象 | 人类 | 模型嵌入 | **AI代理 + 人类开发者** |
| 访问方式 | Web UI | SDK调用 | **MCP标准协议（AI原生） + CLI + Web管理后台** |
| 结构规范 | 自由格式 | 无结构 | **强Schema（标准化文档类型）** |
| 存储方案 | 云服务/私有部署 | 本地文件 | **SQLite + FTS5 单文件存储** |
| 团队协作 | 在线协作 | 无协作能力 | **Git原生同步，纯文本版本管理** |
| 部署成本 | 账号/服务器 | 安装配置 | **`pip install` 开箱即用，零配置** |
| 数据隐私 | 云服务存储 | 本地 | **完全本地存储，无云服务，无遥测** |

---

## ✨ 核心能力详解
### 1. 📐 强Schema标准化与结构化Metadata
内置9种开箱即用的文档类型，覆盖技术团队90%的知识沉淀场景，所有类型支持结构化Typed Metadata并可通过Python子类扩展：
| 文档类型 | ID前缀 | 用途说明 | 结构化Metadata亮点 |
|---|---|---|---|
| `project` | `proj` | 项目/仓库说明文档 | 记录技术栈、负责人、状态、部署流程 |
| `decision` | `dec` | 架构决策记录(ADR) | 结构化记录 `status`(accepted/superseded)、决策人、替代方案 |
| `lesson` | `lesson` | 经验教训/踩坑记录 | 结构化记录 `severity`、根因归类、避坑准则 |
| `glossary` | `glossary` | 术语表 | 统一业务/技术专有名词标准定义 |
| `person` | `person` | 人员档案 | 团队成员技术栈、模块分工与联系方式 |
| `faq` | `faq` | 常见问题 | 沉淀高频问答，消除重复咨询 |
| `api` | `api` | API接口文档 | 结构化记录 HTTP Method、Path、鉴权与限流要求 |
| `runbook` | `runbook` | 运维手册(SOP) | 标准化运维及应急处置步骤 |
| `release` | `release` | 发布日志 | 版本号、变更清单、影响范围与回滚步骤 |

**优势**：
- **真实结构化元数据**：支持 JSON 字段与 Frontmatter 无损互转，AI 可精确抽取与检索事实；
- 自动生成稳定ID（比如 `dec/use-sqlite-fts5`），可被跨文档可靠引用；
- 支持自定义扩展文档类型，满足团队个性化需求。

---

### 2. ⚡ Section 级细粒度切片与 Token 保护
传统知识库全量返回整篇文档极易挤占 Agent 上下文空间，造成 Token 浪费。
- **章节按需获取**：`kb_get(id="...", section="Architecture")` / `kb get --section "..."` 仅提取目标标题章节；
- **智能目录感知**：未指定章节时或章节不存在时，自动返回可用章节目录清单；
- **精准降耗**：将长文档上下文消耗降低 70%~90%。

---

### 3. 🔍 多模式智能搜索
支持四种搜索模式，满足不同场景的查询需求：
- **词法搜索（默认）**：基于SQLite FTS5，BM25排序，精准匹配关键词，适合查找确定的技术点
- **模糊搜索**：基于trigram索引，容错拼写错误、缩写、别名，适合模糊记忆的查询
- **语义搜索（可选）**：安装 `sqlite-vec` 扩展后支持，支持自然语言语义匹配，适合模糊问题查找相关知识
- **混合搜索**：融合词法、模糊、语义三种搜索结果，使用倒数排名融合（RRF）算法，提供最佳综合搜索结果

**搜索能力特性**：
- 支持按文档类型、标签过滤
- 支持 `vault="*"` 跨库联合搜索
- 自动关联相关文档的反向链接
- 搜索结果返回完整的结构化信息，AI可以直接使用
- 支持图扩展选项，自动显示相关文档

---

### 4. 📜 完整版本控制与审计
所有文档的增删改操作都会被完整记录：
- 查看任意文档的完整修改历史，每次变更都有版本号
- 支持版本对比，字段级差异展示，清楚知道改了什么
- 支持恢复到任意历史版本，误修改可以一键回滚
- 软删除机制，删除的文档可以随时恢复，不会丢失数据
- 审计日志记录所有操作，支持审计追踪

---

### 5. 🔗 类型化知识图谱
文档之间可以创建带关系的链接：
- 支持10种标准关系类型：`relates-to`、`supersedes`、`superseded-by`、`depends-on`、`blocks`、`implements`、`references`、`governs`、`owned-by`、`tagged-with`
- 支持自定义关系类型（在配置文件中定义）
- 自动生成反向链接，查找某个决策影响哪些项目，某个Bug关联哪些经验
- 支持影响分析：当文档变更时，自动识别所有下游受影响的文档
- 支持决策演进链追踪：查看决策如何随时间演变
- 支持知识图谱可视化（Web管理后台），直观看到知识之间的关联关系
- 链接完整性校验，自动检测失效链接

---

### 6. 🤝 Git原生团队协作 & 智能三方合并
完全基于Git的团队同步机制，学习成本为零：
- **智能元数据合并 (3-Way Merge)**：多人同时修改文档时，对 `tags` / `aliases` / `links` / `metadata` 自动做集合并集合并，杜绝 Git 冲突；
- **自动化文件监听 (Auto-Watcher)**：提供 `kb watch` 命令，支持事件驱动（Linux inotify、macOS FSEvents、Windows ReadDirectoryChangesW）和轮询模式，本地编辑 Markdown 文件实时增量同步至 SQLite 数据库；
- **自动链接提取**：文档添加或更新时，自动从Markdown正文中提取文档引用（`[text](id)` 和 `` `id` `` 语法），创建 `references` 关系链接；
- 数据库文件本地存储，不会提交到Git，每个成员有独立的本地实例；
- 完全兼容现有Git工作流，支持PR评审、分支管理、Code Owner等机制。

---

### 7. 🗄️ 多 Vault 隔离与跨库联合检索
支持创建多个独立的知识库，数据完全隔离且支持 MCP 动态路由：
- 不同项目、不同团队使用独立的vault，互不干扰；
- **跨库联合搜索**：在 MCP 中支持 `kb_search(query="...", vault="*")`，一次调用同时检索全局公共库与当前项目私有库；
- **动态单库切换**：`kb_get(id="...", vault="work")` 无需重启服务，随时精准路由；
- **Vault管理**：支持创建、切换、列出vault，以及Git同步操作。

---

### 8. ⏰ 智能计划任务
内置APScheduler任务调度器，支持自动维护任务：
- **自动提交**：定时导出vault变更并Git提交（默认30分钟）
- **自动嵌入**：定时处理嵌入队列（默认5分钟）
- **自动重建索引**：定时重建FTS5索引（默认每天凌晨3点）
- **健康检查**：定时运行健康检查（默认每周一上午9点）
- **清理任务**：定时清理软删除超过30天的文档（默认每周日凌晨2点）
- 支持自定义任务间隔和Cron表达式
- 任务失败自动禁用，支持手动重新启用
- 跨进程安全，防止并发执行

---

### 9. 🌐 MCP协议原生支持 (25工具/13资源/7提示)
完全兼容MCP（Model Context Protocol）标准协议，任何支持MCP的客户端（Claude Desktop、Cursor、Composio等）都可以直接接入，AI自动获得以下能力：
#### 25个内置工具
| 工具名称 | 功能说明 | AI使用场景 |
|---|---|---|
| `kb_add` | 创建文档 (支持 `aliases`, `metadata`, `source`, `id`) | AI学习到新知识后，自动沉淀结构化事实到知识库 |
| `kb_get` | 获取文档详情 (支持 `vault` 参数) | AI按需拉取完整内容或指定章节，节省上下文 Token |
| `kb_update` | 更新文档 (支持 `aliases`, `metadata`, `source`) | 文档内容过时，AI自动更新补充 |
| `kb_delete` | 软删除文档 | 废弃的文档，AI可以删除 |
| `kb_restore` | 恢复到指定版本 | 误修改后回滚 |
| `kb_restore_deleted` | 恢复软删除文档 | 误删后恢复 |
| `kb_search` | 全文搜索 (支持 `mode`, `vault` 通配符) | AI遇到问题时，先搜索团队知识库有没有相关解决方案 |
| `kb_list` | 按类型/标签/Vault筛选文档 | AI查看所有架构决策、所有项目信息等 |
| `kb_link` | 创建带类型的文档链接 | AI发现文档之间的关联关系，自动建立链接 |
| `kb_unlink` | 移除链接 | 关联关系失效时删除 |
| `kb_rel_spec` | 列出或查看关系类型详情 | AI了解可用的文档关系类型 |
| `kb_query_relations` | 多跳图遍历 | AI分析文档之间的复杂关系网络 |
| `kb_impact` | 影响分析 | AI评估文档变更的影响范围 |
| `kb_decision_chain` | 决策演进链追踪 | AI了解决策的演变历史 |
| `kb_expand` | 1跳图扩展 | AI查看文档的直接关联文档 |
| `kb_similar` | 嵌入相似度搜索 | AI找相关上下文，避免重复沉淀 |
| `kb_duplicates` | 近似重复检测 | AI发现并合并重复知识 |
| `kb_embed_status` | 嵌入队列状态 | AI了解嵌入处理进度 |
| `kb_embed_retry` | 重新排队失败嵌入 | AI修复嵌入失败问题 |
| `kb_schedule_list` | 列出计划任务 | AI查看自动任务配置 |
| `kb_schedule_status` | 调度器状态 | AI了解任务运行状态 |
| `kb_schedule_run` | 手动触发任务 | AI立即执行维护任务 |
| `kb_schedule_history` | 任务执行历史 | AI查看任务执行记录 |
| `kb_history` | 版本历史 | AI想知道某个决策的变更过程 |
| `kb_diff` | 版本差异对比 | AI查看文档修改了什么内容 |
| `kb_doctor` | 健康检查 | AI先确认知识库结构和索引是否正常 |
| `kb_diff_check` | Git差异分析 | AI分析代码变更并推荐相关文档 |

> 批量导入/导出、`prune`、`reindex`、vault 和 Git 同步属于 CLI/Admin 生命周期能力，不会作为 MCP 文件系统工具暴露。
kb
#### 13个结构化资源
| 资源URI | 返回内容 |
|---|---|
| `kb://doc/{type}/{slug}` | 完整文档信息 |
| `kb://links/{type}/{slug}` | 文档的所有入站和出站链接 |
| `kb://types` | 所有文档类型的列表 |
| `kb://stats` | 知识库统计信息 |
| `kb://graph/{type}/{slug}` | 以该文档为中心的知识图谱 |
| `kb://graph/{type}/{slug}/{depth}` | 指定深度的知识图谱 |
| `kb://list` | 所有文档列表 |
| `kb://list/{type}` | 按类型筛选的文档列表 |
| `kb://changes` | 最近变更记录 |
| `kb://history/{type}/{slug}` | 指定文档的版本历史 |
| `kb://search/{query}` | 搜索结果 |
| `kb://export/{type}/{slug}` | 导出文档为Markdown |
| `kb://help/{doc}` | 帮助文档 |

#### 7个交互Prompt
| Prompt名称 | 用途 |
|---|---|
| `new-doc` | 引导式创建新文档 |
| `link-analysis` | 分析文档链接关系 |
| `search-guide` | 智能搜索助手 |
| `import-docs` | 导入文档指引 |
| `doctor` | 知识库健康检查 |
| `maintenance` | 知识库维护指导 |
| `onboarding` | 新手上手指南 |

---

## 🚀 快速开始使用
### 🔧 安装
```bash
pip install kb-mcp-lite

# 可选安装语义搜索支持（需要SQLite扩展支持）
pip install kb-mcp-lite[vec]

# 可选安装事件驱动文件监听（Linux/macOS/Windows原生支持）
pip install kb-mcp-lite[v0_8]

# 安装所有可选依赖
pip install kb-mcp-lite[vec,v0_8]
```

### 个人用户基础使用
#### 1. 初始化知识库
```bash
kb init
```
会在默认路径 `~/.local/share/kb-mcp/` 创建默认vault的SQLite数据库。

#### 2. 添加第一个文档
```bash
kb add --type project \
       --title "kb-mcp-lite" \
       --tags "mcp,knowledge-base,python" \
       --body "面向AI代理的轻量级本地知识库，基于SQLite + FTS5 + MCP协议开发。"
```

#### 3. 搜索文档
```bash
# 默认搜索
kb search "MCP 知识库"

# 按类型过滤
kb search "sqlite" --type decision

# 模糊搜索
kb search "ft5" --fuzzy
```

#### 4. 查看已有文档
```bash
# 查看所有文档
kb list

# 按类型过滤
kb list --type lesson

# 按标签过滤
kb list --tags "sqlite,bug"
```

#### 5. 更多CLI命令
```bash
# 查看文档详情（完整）
kb get <文档ID>

# 按章节获取文档内容（仅提取特定标题内容，节省Token）
kb get <文档ID> --section "Architecture"

# 更新文档
kb update <文档ID> --title "新标题"

# 删除文档
kb delete <文档ID>

# 查看版本历史
kb history <文档ID>

# 对比版本差异
kb diff <文档ID> --v1 1 --v2 2

# 恢复到指定版本
kb restore <文档ID> --version 2

# 创建文档链接
kb link --from <文档ID1> --to <文档ID2> --rel "relates-to"

# 查看文档链接
kb links <文档ID>

# 影响分析
kb impact <文档ID>

# 决策演进链追踪
kb chain <决策文档ID>

# 关系类型管理
kb rel list                    # 列出所有标准关系类型
kb rel show supersedes         # 查看特定关系详情

# 自动监听 Markdown 目录实时增量同步入库
kb watch --interval 1.0

# 启动Web管理后台
kb admin start

# 计划任务管理
kb scheduler list              # 列出所有任务
kb scheduler status            # 查看调度器状态
kb scheduler run auto-commit   # 手动触发任务
kb scheduler history           # 查看执行历史

# 健康检查
kb doctor

# 统计信息
kb stats

# 清理软删除文档
kb prune --older-than 30

# Git差异分析
kb diff-check
```

---

### 👥 团队协作配置
#### 首次配置团队知识库
1. **管理员创建团队Git仓库**（空仓库即可）
2. **管理员本地初始化vault并关联Git**
   ```bash
   # 创建团队vault
   kb vault create team --desc "XX团队公共知识库"
   kb vault switch team
   
   # 克隆团队Git仓库到本地
   git clone <团队Git仓库地址> ~/team-kb
   
   # 关联vault和Git同步目录
   kb vault init-git --sync-dir ~/team-kb
   
   # 导出已有文档到Git目录并提交
   kb vault commit -m "初始化团队知识库"
   kb vault push
   ```

#### 新成员加入
```bash
# 1. 克隆团队知识库Git仓库
git clone <团队Git仓库地址> ~/team-kb

# 2. 创建本地vault
kb vault create team --desc "XX团队公共知识库"
kb vault switch team

# 3. 关联Git同步目录
kb vault init-git --sync-dir ~/team-kb

# 4. 拉取并导入所有文档
kb vault pull
```

#### 日常协作流程
```mermaid
graph LR
A[AI自动/手动添加修改文档] --> B[拉取最新变更]
B --> C{是否有冲突?}
C -->|是| D[解决Markdown文件冲突]
C -->|否| E[提交本地变更]
D --> E
E --> F[推送到远程仓库]
```

日常操作命令：
```bash
# 写文档前先拉取最新
kb vault pull

# AI添加/修改文档后，提交变更
kb vault commit -m "添加XX项目部署流程文档"

# 推送到远程仓库
kb vault push
```

#### AI工具自动同步配置
如果希望AI调用`kb add`添加文档后自动同步到Git，可以配置post-hook脚本，在`~/.config/kb-mcp/config.yaml`中添加：
```yaml
hooks:
  post_add: "kb vault commit -m 'AI自动添加文档: {doc_title}' && kb vault push"
  post_update: "kb vault commit -m 'AI自动更新文档: {doc_title}' && kb vault push"
```

---

### 🤖 MCP客户端接入配置
#### Claude Desktop 配置
编辑 `~/.config/claude_desktop_config.json` 添加：
```json
{
  "mcpServers": {
    "kb": {
      "command": "kb",
      "args": ["serve"]
    }
  }
}
```
重启Claude后，AI就可以直接访问你的知识库了。

#### Cursor 配置
在Cursor设置中找到MCP服务器配置，添加：
- 名称：`kb`
- 命令：`kb`
- 参数：`["serve"]`

#### 指定使用某个vault
如果有多个vault，可以指定启动时使用的vault：
```json
"args": ["serve", "--vault", "team"]
```

#### Cline 配置
在Cline设置中找到MCP服务器配置，添加：
- 名称：`kb`
- 命令：`kb`
- 参数：`["serve"]`

#### OpenCode 配置
在OpenCode配置文件中添加：
```json
{
  "mcpServers": {
    "kb": {
      "command": "kb",
      "args": ["serve"]
    }
  }
}
```

---

### 🔌 高级用法
#### 自定义文档类型
```python
from kb_mcp_lite.schema import Document, Field

class ApiDoc(Document):
    """API接口文档类型"""
    type: str = "api"
    id_prefix: str = "api"
    
    # 自定义字段
    endpoint: str = Field(description="接口路径")
    method: str = Field(description="HTTP方法")
    version: str = Field(description="接口版本")
    
    class Config:
        schema_extra = {
            "example": {
                "title": "用户获取接口",
                "endpoint": "/api/v1/user/{id}",
                "method": "GET",
                "version": "v1",
                "tags": ["user", "api"],
                "body": "接口返回用户的基本信息..."
            }
        }
```
注册后就可以使用 `kb add --type api` 创建这种类型的文档。

#### 批量导入现有Markdown文档
```bash
# 导入目录下所有Markdown文件
kb import ./docs/

# 试运行，查看会导入什么，不实际写入
kb import ./docs/ --dry-run

# 导入后输出JSON格式报告
kb import ./docs/ --json
```
要求Markdown文件顶部包含YAML frontmatter，至少有`type`和`title`字段。

#### 导出知识库
```bash
# 导出所有文档到指定目录
kb export ./export_dir/

# 强制覆盖已有文件
kb export ./export_dir/ --force
```

---

## 💡 最佳实践
### 文档命名与分类规范
1. **标题清晰准确**：用动宾结构或者问题式标题，比如「Redis缓存击穿解决方案」而不是「Redis笔记」
2. **标签统一规范**：所有标签小写，用短横线分隔，比如 `redis-cache`, `bug-fix`
3. **关联关系完整**：创建文档时主动关联相关文档，比如决策记录关联对应的项目，经验教训关联对应的Bug决策
4. **及时更新**：文档过时后及时更新，不要保留错误信息

### 团队协作规范
1. **提交信息规范**：`kb vault commit -m "提交信息"` 要清晰说明修改内容
2. **PR评审机制**：重要文档变更走PR评审，保证知识质量
3. **定期清理**：每个季度运行一次 `kb doctor` 检查知识库健康度，清理失效文档和链接

### AI使用建议
1. 要求AI解决问题前先搜索知识库，优先使用已有方案
2. 解决完新问题后，要求AI自动沉淀到知识库作为经验
3. 定期让AI整理知识库，优化结构、补充关联、更新过时内容

---

## 🛠️ 开发指南
### 本地开发环境搭建
```bash
git clone https://github.com/HelloTomBruce/kb-mcp-lite
cd kb-mcp-lite

# 安装依赖（推荐使用uv）
uv sync --extra dev

# 运行测试
uv run pytest

# 代码检查
uv run ruff check .
uv run mypy src/

# 格式化代码
uv run ruff format .
uv run ruff check --fix .
```

### 项目结构说明
```
src/kb_mcp_lite/
├── cli.py              # Click CLI (28 commands)
├── mcp_server.py       # FastMCP server (25 tools, 13 resources, 7 prompts)
├── schema.py           # Document, Link, SearchHit, TypeRegistry, exceptions
├── store.py            # Store Protocol (interface contract)
├── store/              # SQLite存储核心
│   ├── sqlite.py       # SqliteStore (composes 4 mixins)
│   ├── search.py       # SearchMixin — FTS5 + vec0 hybrid search
│   ├── embedding.py    # EmbeddingMixin — vec0 vectors, similarity, duplicates
│   ├── versioning.py   # VersioningMixin — history, snapshots, diff, restore
│   ├── maintenance.py  # MaintenanceMixin — doctor, prune, stats, subgraph
│   ├── embedding_queue.py # EmbeddingQueue — async queue with state machine
│   └── connection.py   # Shared sqlite3 connection factory
├── md_io.py            # Markdown frontmatter parser + bulk import/export
├── vault.py            # Multi-vault management
├── admin/              # FastAPI web UI
│   ├── routes_docs.py  # Document CRUD + search
│   └── routes_meta.py  # Overview, links, graph, settings
├── migrations/         # Forward-only SQL migration runner
├── config.py           # XDG config loader
├── embedder.py         # OpenAI-compatible embedding client
├── worker.py           # Background embedding worker thread
├── watcher.py          # File watcher (event/poll modes)
├── scheduler.py        # APScheduler task scheduler (5 built-in tasks)
├── graph_query.py      # Multi-hop graph query engine (BFS)
├── relations.py        # Typed relation vocabulary + impact analysis
├── link_parser.py      # Body-level reference parser [text](id)
├── context_guard.py    # Git diff → relevant decisions/lessons
├── merge.py            # 3-way Markdown merge
├── migrations.py       # Forward-only SQL migration runner
└── concurrency/
    └── write_lock.py   # Cross-process flock-based write lock
```

---

## 📌 状态说明
当前处于**Beta测试阶段**：
- API和存储格式从v0.5.0开始已经稳定，不会有破坏性变更
- v0.8.0 新增：auto-link、关系词汇、调度器、嵌入队列、多跳图查询等
- 生产环境使用建议锁定版本：`kb-mcp-lite>=0.8,<0.9`
- 欢迎提交Issue和PR，贡献代码请查看 [CONTRIBUTING.md](./CONTRIBUTING.md)

---

## 📄 许可证
MIT License，可自由使用、修改、分发，保留版权声明即可。

---

## 🔗 相关链接
- **GitHub仓库**：https://github.com/HelloTomBruce/kb-mcp-lite
- **PyPI包**：https://pypi.org/project/kb-mcp-lite/
- **问题反馈**：https://github.com/HelloTomBruce/kb-mcp-lite/issues
- **更新日志**：https://github.com/HelloTomBruce/kb-mcp-lite/releases
- **架构文档**：[docs/architecture.md](./docs/architecture.md)
- **CLI参考**：[docs/cli-reference.md](./docs/cli-reference.md)
- **关系词汇**：[docs/relation-vocabulary.md](./docs/relation-vocabulary.md)
- **计划任务**：[docs/scheduler.md](./docs/scheduler.md)
