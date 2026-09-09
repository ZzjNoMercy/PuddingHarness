# Agent-native Knowledge Platform 独立服务与可迁移 Workspace 规范

> 状态：Approved Direction v0.7 / Phase 0A Execution Baseline
> 日期：2026-09-02
> 决策范围：Knowledge Catalog、异构知识处理 Pipeline、Semantic Assets、Knowledge Dataset、RAG、LLM Wiki、表格问数、Vanna/NL2SQL、MCP 对外协议、可迁移 Workspace，以及 PuddingClaw Harness 的最终解耦边界

### 修订记录

| 版本 | 日期 | 内容 |
| --- | --- | --- |
| v0.1 | 2026-09-02 | 建立产品边界、Capability、MCP、Package、Workspace 和初步迁移方案 |
| v0.2 | 2026-09-02 | 修正 Wiki compile、TableAssetCatalog、vendored Vanna 等事实；补充 Catalog DB 拆分、双向依赖网、DeepAgents 接线中心、协议字段清理、完整子域盘点、前端、分发部署、gbrain、模型 Provider、评测保留和迁移中间态 |
| v0.3 | 2026-09-02 | 纳入二轮审核收口项：共享 Queue Lease 原语、Deep Research、Intent Router、Knowledge Skills、配置/凭据逐项归属、Wiki Compiler 明确迁移阶段、数据库 Tool 数量修正和 Asset derivative/range read REST 契约 |
| v0.4 | 2026-09-02 | 冻结不可逆迁移顺序：原仓库解耦、同进程 shadow、sidecar 灰度、保留历史抽取独立仓库、关闭回滚窗口、最后删除；补充数据库原子切换和旧数据清理门禁 |
| v0.5 | 2026-09-02 | 补齐 github-monitor 知识耦合 Skill，修正 Queue Repository 归属，重排 Phase 1–11 连续编号，并明确异构存储切换的 revision 指针原子性与混合 revision 容错 |
| v0.6 | 2026-09-02 | 终态改为三仓库拓扑：PuddingClaw 原仓库永久保留为集成产品与回滚源，新建 `puddingknowledge`（Platform）与 `puddingharness`（纯 Harness）两个仓库；Phase 11 由“在 PuddingClaw 中删除代码”改为“PuddingHarness 首发纯 Harness 发行 + PuddingClaw 归档收口”；新增存量安装升级路径与验收主体术语约定 |
| v0.7 | 2026-09-02 | 最终方案收口：PuddingHarness RC 前置验证、有状态升级/回退状态机、capability/release/installation 三类回滚窗口、三仓内公共契约归属、混合文件抽取清单、存量升级验收，并清除“PuddingClaw 原地变纯 Harness/删除实现”的残留表述 |

### v0.2 审核结论

第一轮审核确认产品方向和对外契约可继续采用，但 v0.1 对实际拆分规模的覆盖只有约 50%–60%。v0.2 将以下三项提升为 Phase 0 前置门禁，而不是后续实现细节：

1. Catalog SQLAlchemy Base、表归属和迁移链拆分；
2. `knowledge ↔ analytics ↔ tools ↔ graph/harness` 双向依赖环拆解；
3. Electron、Deploy CLI、Docker Compose、Frontend 和协议字段的发行面迁移。

在这三项形成可执行设计和验证脚本前，不开始物理拆库或删除 Claw 业务代码。

二轮审核结论：v0.2 已达到“可批准方向、进入 Phase 0”的标准。v0.3 的新增内容是 Phase 0A inventory 和执行边界收口；v0.4 进一步将迁移先后关系升级为硬门禁；v0.5 修复 inventory 和执行编号歧义；v0.6 将终态从“两仓库 + 原地删除”改为“三仓库 + 归档收口”；v0.7 补齐三仓实施、首发和有状态回退闭环。后续修订均不改变已批准的产品方向。

## 1. 决策摘要

本方案将 PuddingClaw 当前分散在 `knowledge/`、`analytics/`、`tools/` 和 DeepAgents Middleware 中的知识与数据能力，重构为独立的 **Agent-native Knowledge Platform**。

平台向外提供两类等价的知识交付方式：

1. **在线 Knowledge Runtime**：通过 REST API 和 MCP Resources/Tools 向任意外部 Agent 提供实时知识查询服务。
2. **离线 Knowledge Workspace**：将同一份 Knowledge Package 编译为可直接交给 Codex、Claude Code 或其他文件系统型 Agent 使用的 Workspace。

`puddingharness`（本文目标态中的 Claw）的产品目标是纯 Harness；PuddingClaw 原仓库按 1.1 永久保留完整集成实现：

- 不内置 `knowledge_list`、`knowledge_search`、`knowledge_read`、`knowledge_query`；
- 不内置 `document_rag_query`、`wiki_query`、`table_query`、`database_nl2sql`、`database_execute_readonly`；
- 不链接或初始化 LlamaIndex、Milvus、Vanna、Wiki Runtime、数据库 Query Runtime；
- 不维护 Knowledge Catalog、Semantic Assets、Knowledge Collection 或知识索引；
- 仅通过用户配置或安装的外部 MCP Server 等扩展发现并调用知识能力。

目标依赖方向固定为：

```text
External Agent / Claw Pure Harness / BI / CLI
                    │
             MCP / REST / Workspace
                    │
                    ▼
          Agent-native Knowledge Platform
          ├─ Knowledge Catalog
          ├─ AI-native Processing Pipeline
          ├─ Semantic Assets
          ├─ Knowledge Collections
          ├─ Query Capabilities
          └─ Package / Workspace Publisher
                    │
                    ▼
       Files / Wiki / Excel / Database / APIs
```

本规范取代 `docs/plans/2026-08-18-puddingharness-deepagents-decoupling-plan.md` 中“Knowledge 作为 Harness 内置 Extension，向 Harness 注册业务 Tool、Middleware、Prompt 和虚拟资源”的目标形态。旧方案仍可作为迁移期的依赖清理参考，但不是最终产品边界。

### 1.1 目标仓库拓扑与术语约定

终态是三个仓库，而不是“两仓库 + 在 PuddingClaw 中原地删除业务代码”：

| 仓库 | 角色 |
| --- | --- |
| `PuddingClaw`（本仓库） | 永久保留。迁移期是唯一的集成产品和开发主干；拆分完成后成为完整的 legacy/回滚源，进入维护或归档模式，**永不删除其中的 Knowledge 实现** |
| `puddingknowledge` | 新建。Agent-native Knowledge Platform 的独立仓库，含 Runtime、Console、deploy assets、contracts 和测试 |
| `puddingharness` | 新建。纯 Harness 仓库，由 PuddingClaw 抽取/重建而来，首发即不含任何 Knowledge/Analytics 实现 |

术语约定：本文中“Claw”描述**目标态行为**时一律指 `puddingharness`（例如“Claw 源码不 import `knowledge`”“Claw 安装包不携带 Platform Console”）；描述**迁移期现状或 legacy 路径**时指 PuddingClaw 仓库中的现有实现。PuddingClaw 仓库永远 import `knowledge`/`analytics`，因此不参与“无 Knowledge 依赖”类验收；该类验收只作用于 `puddingharness` 和 `puddingknowledge`。

这一拓扑把全方案风险最高的操作——在仍在服务用户的仓库里物理删除代码——替换为“新仓库首发 + 旧仓库归档”，回滚从“恢复已删除的代码”简化为“继续发行 PuddingClaw”。

## 2. 产品定义

### 2.1 一句话定义

Agent-native Knowledge Platform 是一个将异构知识经过 AI-native Pipeline 加工为 Agent 友好的知识资产、语义资产和 Knowledge Collection，并通过在线协议或离线 Workspace 向任意 Agent 提供发现、读取、检索和计算能力的平台。

### 2.2 平台提供的四项核心价值

1. **AI-native 数据处理 Pipeline**：导入、解析、结构化、清洗、语义增强、索引、评测和发布。
2. **Agent 友好的语义资产**：用机器可读元数据和人类可读 Markdown 描述知识边界、业务含义、查询方法、约束和来源。
3. **Agent 友好的 Knowledge Collection**：将文件、数据库、Wiki、语义定义和查询能力组织为可选择、授权、版本化和迁移的知识集合。
4. **Agent 友好的知识查询工具**：提供 RAG、Wiki Query、Table Query、NL2SQL 等稳定、结构化、可追溯的查询契约。

### 2.3 “分析模型”的新定位

当前 `AnalyticsModel` 不再是平台顶层抽象，而是结构化 Knowledge Dataset 的一个子类型；顶层交付单元统一称为 Knowledge Collection：

```yaml
kind: relational-analytics
```

其他 Dataset 类型包括：

```yaml
kind: document-rag
kind: wiki
kind: spreadsheet
kind: relational-analytics
kind: hybrid
```

因此，现有智能问数、Vanna、NL2SQL、语义指标和 SQL Guardrail 全部保留，但归入结构化 Knowledge Dataset 和 Database Query Capability；文档、Wiki、混合知识等对 Agent 发布的边界统一归入 Collection。

## 3. 目标与非目标

### 3.1 目标

1. 统一声明平台拥有的文档、PDF、图片、Excel、CSV、数据库、Wiki、API 和语义知识。
2. 所有查询能力脱离 DeepAgents、LangChain Tool 和 Claw Session/Run/Goal 独立运行。
3. 同一 Knowledge Package 可以发布为在线 Runtime，也可以导出为 Workspace。
4. MCP Server 同时提供可发现的 Resources 和可由 Agent 调用的 Tools。
5. 查询结果使用统一的 Evidence、Provenance、Warning 和 Error Contract。
6. Knowledge Collection、结构化 Knowledge Dataset、Semantic Assets 和查询工具具有稳定版本与兼容性声明。
7. 数据库执行始终受服务端数据权限、只读校验、表范围、资源配额和审计约束。
8. Claw 在未连接任何 Knowledge Platform 时仍可作为纯 Harness 正常工作。
9. Knowledge Platform 在 Claw 不存在或不可用时仍可被其他 Agent 和应用完整使用。

### 3.2 非目标

1. 不将 MCP 作为平台内部领域模型或唯一内部 API。
2. 不要求所有知识都转换成向量索引。
3. 不把 PDF、Excel 或数据库强制转换成同一种物理存储。
4. 不把通用任务规划、Goal、Todo、SubAgent、会话压缩或 Harness 权限工作流迁入 Knowledge Platform。
5. 不允许外部 Agent 默认提交任意 SQL 直接执行。
6. 不在可迁移包中包含数据库密码、API Key 或租户密钥。
7. 不将 Milvus、Vanna 或 LlamaIndex 的底层索引文件视为唯一事实源。
8. 第一版不提供 Agent 可调用的知识写入、删除或管理 Tool；管理面通过受控 REST/Admin API 提供。

## 4. 核心领域模型

### 4.1 Knowledge Space

Knowledge Space 是租户、组织、项目或业务域级的知识边界，负责：

- 资产与数据集的命名空间；
- 调用者和权限；
- 数据源连接；
- 发布策略；
- 审计与配额。

### 4.2 Knowledge Asset

Knowledge Asset 是最小可寻址知识对象。建议统一字段：

```yaml
id: asset_...
space_id: space_...
kind: document | image | spreadsheet | table | database | wiki_page | api | semantic
title: 年度报告
description: 2025 年公司年度报告
mime_type: application/pdf
source:
  type: upload | filesystem | connector | database | generated
  uri: source-specific-uri
revision: source revision or content digest
content_digest: sha256:...
derivatives:
  - kind: normalized_markdown
    uri: knowledge://spaces/acme/assets/asset_x/derivatives/markdown
permissions: ...
metadata: ...
```

原始资产和派生产物必须区分：

- PDF 原件是 Asset；MinerU/OCR Markdown、图片和页码映射是 Derivatives。
- Excel 原件是 Asset；Sheet、Profile、Schema、Parquet 快照是 Derivatives 或子资产。
- 数据库连接是 Live Asset；Schema、Table Profile、DDL、枚举观测是可版本化派生产物。
- Wiki 页面既可以是派生产物，也可以作为已发布的独立 Asset。

### 4.3 Semantic Asset

Semantic Asset 是供 Agent 理解和正确使用知识的权威定义，包括：

- 描述、别名、标签；
- 指标、维度、粒度、实体和关系；
- 适用问题与禁止推断；
- 字段映射和业务口径；
- 查询建议和验证规则；
- 来源、版本和可信度。

现有 `measure`、`dimension`、`grain`、`relation` 继续保留，同时允许扩展：

```text
concept
entity
metric
dimension
grain
relation
policy
query_example
data_contract
```

Semantic Asset 必须同时具备：

1. 机器可读 frontmatter/schema；
2. Agent 可直接阅读的 Markdown 正文；
3. 稳定 ID 和内容摘要；
4. 对原始证据的引用。

### 4.4 Knowledge Collection

Knowledge Collection 是向 Agent 发布、授权和迁移的主要单元。它引用一组 Knowledge Assets、Semantic Assets 和 Capabilities；其中表格、数据库等结构化子集仍使用 Knowledge Dataset：

```yaml
id: collection_sales
name: 销售经营知识
version: 1.2.0
kind: hybrid
description: 销售制度、产品资料、月度 Excel 与经营数据库

assets:
  - asset_sales_policy
  - asset_product_manual
  - asset_monthly_excel
  - asset_sales_database

semantic_assets:
  - metric:revenue
  - dimension:channel
  - relation:product_order

capabilities:
  - knowledge_search
  - document_rag_query
  - wiki_query
  - table_query
  - database_nl2sql

freshness:
  mode: mixed
permissions:
  policy_ref: policy_sales_read
```

Collection 不是模型参数集合，也不是某个索引；它是 Agent 可消费的知识产品边界。结构化 Dataset 是 Collection 内受约束的数据子集或查询绑定。

### 4.5 Knowledge Package

Knowledge Package 是 Collection 的不可变、可验证、可迁移制品。它使用语言无关的 YAML/JSON/Markdown 和标准文件格式定义，不等同于 Python 包或 TypeScript 包。

### 4.6 Knowledge Deployment

Knowledge Deployment 是 Package 在某个环境中的运行实例，负责绑定：

- 数据库和 API 端点；
- 密钥引用；
- LLM/Embedding/Rerank Provider；
- Milvus、PostgreSQL、对象存储；
- 索引和缓存；
- 租户权限、配额和审计策略。

Package 不因 Deployment 的连接地址和凭据变化而改变内容摘要。

## 5. AI-native Knowledge Processing Pipeline

统一 Pipeline：

```text
Discover / Upload / Sync
          │
          ▼
Register immutable source revision
          │
          ▼
Parse / Extract / Profile
          │
          ▼
Normalize into agent-readable derivatives
          │
          ▼
Semantic enrichment / Wiki compilation / Entity extraction
          │
          ▼
Index / Train / Build query evidence
          │
          ▼
Evaluate / Lint / Validate
          │
          ▼
Publish Dataset version
          │
          ├─▶ Online Knowledge Deployment
          └─▶ Portable Knowledge Workspace
```

### 5.1 各资产类型的标准产物

| 输入 | 权威原件 | Agent 友好派生产物 | 查询能力 |
| --- | --- | --- | --- |
| Markdown/TXT | 原文件 | chunk、标题、引用定位 | `knowledge_read`、`document_rag_query` |
| PDF/DOCX | 原文件 | Markdown、图片、页码映射、chunk | `document_rag_query` |
| Excel/CSV | 原文件 | Sheet、Schema、Profile、可选 Parquet | `table_query` |
| Database | 实时连接 | Schema、DDL、Profile、实体值、语义定义 | `database_nl2sql` |
| Wiki | Published Markdown | Index、关系、Embedding | `wiki_query` |
| API/Connector | 实时端点 | Contract、缓存快照、Schema | 专用或通用 Query Capability |

### 5.2 索引可重建原则

Milvus、LlamaIndex、Vanna 的运行索引是派生状态，不是迁移事实源。Knowledge Package 至少保存重建所需的：

- Markdown/chunk 输入与定位信息；
- DDL；
- 业务文档；
- NL-SQL 示例；
- 实体与字段映射；
- Table Profile；
- Embedding/Parser/Chunker 版本；
- Build manifest 和内容摘要。

Workspace 可以选择携带索引缓存以提高启动速度，但导入方必须能够丢弃缓存并从权威输入重建。

## 6. Platform Capability Contract

Platform Capability 是框架无关的 application service，不是 LangChain `BaseTool`。同一能力由 REST、MCP 或 Workspace Adapter 暴露。

### 6.1 公共查询能力

```text
knowledge_list
knowledge_search
knowledge_read
knowledge_query

document_rag_query
wiki_query
table_query
database_nl2sql
database_execute_readonly
```

### 6.2 `knowledge_list`

用途：渐进式发现 Space、Dataset、Asset 和 Capability，不返回大段正文。

核心输入：

```json
{
  "space_id": "optional",
  "dataset_id": "optional",
  "kind": "optional",
  "cursor": "optional",
  "limit": 50
}
```

核心输出包括稳定 ID、名称、描述、类型、版本、可用 Capability、Resource URI 和 freshness。

### 6.3 `knowledge_search`

用途：跨一个或多个 Dataset 进行统一证据搜索，不直接执行数据库分析。

它可以融合：

- Catalog metadata；
- 文本、图片和向量召回；
- Wiki page；
- Semantic Asset；
- 数据库 Schema/DDL/Profile；
- Query examples。

每条命中必须返回 `asset_id`、`resource_uri`、`locator`、`quote/summary`、`score`、`revision` 和 `matched_by`。

### 6.4 `knowledge_read`

用途：读取精确 Asset、Derivative 或证据片段。

读取大文件必须支持范围或逻辑定位：

```text
page
section
sheet
row range
chunk_id
byte range
```

`knowledge_read` 与 MCP `resources/read` 使用同一底层 `AssetReadService`，避免两套访问规则。

### 6.5 `knowledge_query`

用途：面向不了解底层资产类型的 Agent 提供统一问答入口。

平台根据 Collection manifest、显式 hint、权限和问题类型选择一个或多个 Capability：

```text
Document question  → document_rag_query
Wiki concept       → wiki_query
Spreadsheet        → table_query
Database analytics → database_nl2sql
Hybrid question    → v1 选择一个主引擎并返回可继续查询的 Capability；v2 才进行跨引擎 fusion
```

路由是 Platform 能力，不再由 Claw 的 `ToolIntentRouterMiddleware` 拥有。

`knowledge_query` v1 只做单引擎自动路由，不承担跨 Document/Wiki/Table/Database 的结果联合、冲突消解和 Evidence Fusion。调用方需要联合分析时可以显式调用多个专业 Tool；Platform v2 在建立跨引擎评测基线后再增加受控 orchestration。

### 6.6 `document_rag_query`

用途：对 PDF、Markdown、图片和其他已索引文档执行混合检索。

Platform 内部可以继续使用 LlamaIndex、Milvus、BM25、RRF 和 Reranker，但公共契约不暴露特定供应商对象。

### 6.7 `wiki_query`

用途：查询已发布 Wiki 页面、概念、实体和关系。

读取 Published Wiki，不默认读取 Raw；返回匹配页面、section、引用和关系。Wiki 编译、发布、lint 和退役属于管理/处理 Pipeline，不作为第一版公共 Agent Tool。

### 6.8 `table_query`

用途：对 Excel、CSV、TSV、Parquet 和逻辑表格数据集进行筛选、分组、聚合、排序、趋势和统计计算。

Platform 负责：

- Asset/Sheet 选择；
- Profile 和语义上下文加载；
- 执行引擎选择；
- 代码或表达式安全校验；
- 结果行数和资源限制；
- Evidence 与 Provenance。

### 6.9 `database_nl2sql`

用途：将自然语言问题编译为经过证据约束和只读校验的 SQL Query Plan。

第一版推荐输入：

```json
{
  "dataset_id": "dataset_sales",
  "question": "去年不同渠道的收入是多少？",
  "semantic_asset_ids": ["metric:revenue", "dimension:channel"],
  "limit": 100
}
```

第一版推荐输出：

```json
{
  "query_plan_id": "qp_...",
  "sql": "SELECT ...",
  "dialect": "postgresql",
  "dataset_id": "dataset_sales",
  "dataset_version": "1.2.0",
  "deployment_revision": "deploy-rev-...",
  "semantic_context_hash": "sha256:...",
  "evidence": [],
  "validation": {
    "readonly": true,
    "allowed_tables": true,
    "guardrails_passed": true
  },
  "expires_at": "..."
}
```

Vanna 负责证据检索、DDL/Documentation/Similar SQL/Entity 召回或候选生成，但它是 Platform 的 Infrastructure Provider，不是公共领域对象。

### 6.10 `database_execute_readonly`

第一版只接受 Platform 签发的 `query_plan_id`：

```json
{
  "query_plan_id": "qp_...",
  "page_size": 100
}
```

执行前必须重新验证：

- Query Plan 未过期且属于当前调用者；
- Dataset、Deployment 和数据源版本一致；
- SQL hash 未变化；
- 只包含允许的只读语句；
- 表和字段在授权范围内；
- Guardrail 通过；
- timeout、row cap、scan cap 和并发配额通过。

第一版不允许通过此 Tool 直接提交任意 SQL。未来若确有需求，应通过独立 Tool 和独立 OAuth scope 提供，不得复用本接口隐式开放。

## 7. 统一查询结果协议

所有查询 Capability 使用统一 envelope：

```json
{
  "status": "ok",
  "answer": "...",
  "data": {
    "columns": [],
    "rows": []
  },
  "evidence": [
    {
      "asset_id": "asset_...",
      "resource_uri": "knowledge://spaces/acme/assets/asset_...",
      "locator": {"page": 12, "section": "收入确认"},
      "quote": "...",
      "score": 0.91,
      "revision": "sha256:...",
      "matched_by": ["vector", "bm25"]
    }
  ],
  "provenance": {
    "space_id": "space_acme",
    "dataset_id": "dataset_sales",
    "dataset_version": "1.2.0",
    "capability": "document_rag_query",
    "provider_versions": {}
  },
  "warnings": [],
  "trace_id": "trace_..."
}
```

错误使用稳定分类：

```text
invalid_request
not_found
permission_denied
capability_unavailable
binding_unavailable
index_not_ready
query_generation_failed
query_validation_failed
query_execution_failed
resource_limit_exceeded
stale_query_plan
internal_error
```

错误响应不得要求调用方理解 Claw Session、Run、Goal、LangGraph Interrupt 或内部异常类。

## 8. MCP 对外协议

### 8.1 MCP 是发布 Adapter，不是平台内核

Platform application services 不依赖 MCP SDK。MCP Server 将领域 DTO 映射为 MCP Resources、Tools 和可选 Prompts；REST 和 Workspace 使用同一领域实现。

### 8.2 Resources

MCP Resources 用于声明和读取可寻址知识：

```text
knowledge://spaces/{space_id}/manifest
knowledge://spaces/{space_id}/collections/{collection_id}
knowledge://spaces/{space_id}/assets/{asset_id}
knowledge://spaces/{space_id}/assets/{asset_id}/derivatives/{kind}
knowledge://spaces/{space_id}/semantics/{semantic_asset_id}
knowledge://spaces/{space_id}/query-plans/{query_plan_id}
knowledge://query-results/{query_result_id}/artifact
```

设计约束：

1. `resources/list` 只列出 Space、Collection、精选入口和 Resource Templates，不枚举数百万个 chunk 或数据库行。
2. 大规模资产发现由 `knowledge_list` 和 `knowledge_search` 完成。
3. Resource URI 必须稳定、经过权限校验，不暴露宿主绝对路径。
4. Markdown 和 JSON 优先作为 Agent 友好派生产物；PDF/Excel 原件可以作为 binary resource 提供。
5. Tool Result 中使用 Resource Link 或 Embedded Resource 返回证据和结果。
6. Collection 或资产变化时可使用 `listChanged`/subscription 通知，但客户端不支持通知时仍必须正确工作。
7. QueryResult artifact 只有在 Platform Catalog 存在显式 `QueryResult → Space` 绑定、调用方持有该 Space 的 read scope，且宿主提供经 digest 校验的文件 binding 时，才可通过 `resources/read` 读取；不得从 QueryResult URI、旧 metadata、Session/ToolCall 或默认 Catalog 推断 Space。未绑定必须 fail-closed。
8. Semantic Markdown Resource 只读取独立本地注册表中显式 `active` 的定义；`waiting_for_confirmation`、`rejected`、`retired` 和未挂载注册表均不可读。读取必须持有目标 Space 的 read scope、拒绝 tenant-scoped identity，并以注册表中的稳定 URI 解析定义，不从本地文件路径、旧语义对象、Session/ToolCall 或 Catalog 推断内容。URI 冲突、注册表异常和越界读取均 fail-closed。

### 8.3 Tools

MCP Server 暴露第 6 节定义的公共查询能力。Tool schema 从 Platform DTO/JSON Schema 生成，避免手写第二套契约。

Tool Description 只说明：

- 适用场景；
- 必填 Collection/Asset 范围；
- 是否实时访问数据；
- 是否产生查询计划或执行；
- 权限与成本提示。

Tool Description 不包含 Claw 的工作流、Goal、Receipt 或内部路径说明。

### 8.4 Prompts

MCP Prompts 是可选增强，可提供：

- “探索一个 Knowledge Collection”；
- “引用证据回答”；
- “生成并审核数据库查询”；
- “跨文档和数据库联合分析”。

Prompt 不是业务事实源，也不替代 Collection manifest、Semantic Assets 或服务端校验。

### 8.5 认证与授权

HTTP MCP 使用标准授权机制和短期 Token。授权至少区分：

```text
knowledge:list
knowledge:read
knowledge:search
knowledge:query
database:generate_sql
database:execute_readonly
```

同一 MCP Server 可以根据调用者授权只返回其可访问的 Resources、Collections 和 Tools。

参考：

- MCP Server primitives：https://modelcontextprotocol.io/specification/2025-06-18/server/index
- MCP Resources：https://modelcontextprotocol.io/specification/2025-06-18/server/resources
- MCP Tools：https://modelcontextprotocol.io/specification/draft/server/tools

## 9. Knowledge Package 与 Workspace

### 9.1 Package 目录

```text
knowledge-package/
├── knowledge-package.yaml
├── package-manifest.json
├── README.md
├── assets/
│   ├── originals/
│   └── normalized/
├── semantics/
├── collections/
├── evidence/
├── profiles/
├── evaluations/
├── indexes/
│   └── rebuild-manifest.json
├── bindings.example.yaml
└── checksums.json
```

`knowledge-package.yaml` 至少包含：

```yaml
format: agent-knowledge-package/v1
id: package_sales
version: 1.2.0
spaces:
  - ./spaces/acme.yaml
collections:
  - ./collections/sales.yaml
assets:
  index: ./assets/index.json
semantic_assets:
  index: ./semantics/index.json
capabilities:
  - knowledge_list
  - knowledge_search
  - knowledge_read
  - knowledge_query
  - document_rag_query
  - wiki_query
  - table_query
  - database_nl2sql
bindings:
  contract: ./bindings.example.yaml
indexes:
  rebuild_manifest: ./indexes/rebuild-manifest.json
evaluations:
  root: ./evaluations
```

### 9.2 Workspace 编译目标

Workspace 是 Package 的 Agent 适配结果：

```text
knowledge-workspace/
├── AGENTS.md
├── README.md
├── knowledge-package.yaml
├── skills/
│   ├── knowledge-discovery/SKILL.md
│   ├── document-rag/SKILL.md
│   ├── wiki-query/SKILL.md
│   ├── table-query/SKILL.md
│   └── database-query/SKILL.md
├── bin/
│   └── knowledge
├── assets/
├── semantics/
├── collections/
├── profiles/
├── evaluations/
├── bindings.example.yaml
└── bindings.local.yaml
```

Workspace 中的 `bin/knowledge` 是稳定 CLI Adapter，例如：

```text
knowledge list
knowledge search --query ...
knowledge read --uri ...
knowledge query --collection ... --question ...
knowledge database nl2sql ...
knowledge database execute --query-plan ...
```

Workspace 可以采用两种模式：

1. **snapshot**：携带允许导出的原始数据或快照，在本地完成查询；
2. **connected**：携带 Collection/Semantic/Binding Contract，通过本地凭据绑定实时数据库或远端 Knowledge Runtime。

v1 只承诺完整实现 snapshot。connected 的 manifest/binding schema 可以预留，但在凭据、授权、endpoint trust 和离线失败语义完成专项设计前不作为 v1 验收项。

`AGENTS.md` 和 Skills 只描述如何发现和调用能力，不复制业务事实。业务事实仍来自 Package manifest、Assets、Semantic Assets 和查询结果。

### 9.3 不进入 Package 的内容

- 密码、Token、私钥；
- Claw Session/Run/Goal；
- 宿主机不可解释的绝对路径；
- 未声明许可的数据库数据快照；
- 仅能由原环境读取且无法重建的索引状态；
- Harness Tool receipt 和 LangGraph checkpoint。

## 10. Knowledge Runtime 内部架构

推荐代码边界：

```text
knowledge_platform/
├── domain/
│   ├── spaces.py
│   ├── assets.py
│   ├── semantic_assets.py
│   ├── datasets.py
│   ├── capabilities.py
│   ├── evidence.py
│   └── query_plans.py
├── application/
│   ├── catalog_service.py
│   ├── asset_read_service.py
│   ├── knowledge_search_service.py
│   ├── knowledge_query_router.py
│   ├── document_retrieval_service.py
│   ├── wiki_query_service.py
│   ├── table_query_service.py
│   ├── database_query_service.py
│   └── package_service.py
├── infrastructure/
│   ├── catalog/
│   ├── filesystem/
│   ├── object_store/
│   ├── llamaindex/
│   ├── milvus/
│   ├── vanna/
│   ├── sql/
│   ├── wiki/
│   └── connectors/
└── interfaces/
    ├── http/
    ├── mcp/
    ├── cli/
    └── workspace/
```

依赖方向：

```text
interfaces → application → domain
infrastructure ──────────→ domain/application ports

domain/application ─X→ MCP SDK / FastAPI / LangChain / DeepAgents / Claw graph
```

可以继续使用 Python 实现第一版，因为现有 Pipeline 和查询实现均以 Python 为主；但 Package、REST、MCP 和 JSON Schema Contract 必须保持语言无关。

### 10.1 Catalog 数据库拆分是第一前置条件

当前 Catalog 不是可直接搬迁的 Knowledge Database。`backend/knowledge/models.py` 内约 20 个 ORM Model 共用一个 `Base`；`backend/schema_migrations.py` 直接通过该 `Base.metadata.create_all()` 建立 Core schema；`backend/catalog_migration.py` 也把 Knowledge、Analytics、Headless 和通知表作为一个整体导出。数据库归属已经焊死在单一迁移链中。

现状表归属按目标态拆分为：

| 当前表/模型 | 目标所有者 | 迁移动作 |
| --- | --- | --- |
| KnowledgeBase、Document、SourceConnection、SourceItem、SyncRun | Knowledge Platform | 搬入 Platform Catalog |
| FeishuAppCredential、FeishuUserGrant、FeishuOAuthSession | Knowledge Platform | 搬入 Connector/Vault Catalog；密文和 key reference 单独迁移 |
| ReadLaterItem | Knowledge Platform | 搬入 Web Capture/Ingestion 子域 |
| KnowledgeDatabaseSource | Knowledge Platform | 拆分 secret ref 与非秘密连接元数据后迁移 |
| KnowledgeTableAsset | Knowledge Platform | 搬入 Structured Asset Catalog |
| AnalyticsQueryResult | Knowledge Platform | 改为通用 QueryResult；移除强制 Session/ToolCall 所有权 |
| KnowledgeImportJob/Event | Knowledge Platform | 搬入 Processing Job Catalog |
| SemanticDimensionBuildJob/Event | Knowledge Platform | 搬入 Semantic Authoring Job Catalog；移除 Session/Query 作为主身份 |
| WorkerAccessLog | Claw/Harness | 从 `knowledge.models` 移出，保留在 Harness Core DB |
| TaskNotification | 拆分 | Platform 产生 Domain Event/Job Notification；Claw 通知中心若保留则消费外部事件，不共享 ORM 表 |

数据库拆分原则：

1. 建立 `harness_metadata` 与 `knowledge_metadata` 两个独立 SQLAlchemy MetaData/Base，禁止跨 Base ORM relationship 和 FK。
2. Platform 拥有独立 schema version、migration runner 和备份恢复流程，不再由 Claw `schema_migrations.py` 创建 Platform 表。
3. SQLite 本地形态最终使用两个文件，例如 `claw.sqlite3` 与 `knowledge-platform.sqlite3`；PostgreSQL 过渡期可以先使用同一实例的两个 schema，最终允许分库。
4. Claw 与 Platform 的关联只保存外部稳定 ID，例如 `external_trace_id`、`resource_uri`、`query_result_uri`，不建立数据库 FK。
5. 不采用长期双写。切换使用“停止相关写入 → 一致性快照 → 校验 → 原子切换 → 保留回滚源”的 drain/copy/cutover 流程。
6. 在表尚未物理搬迁前，也必须先完成 Base、Repository 和 Migration ownership 的逻辑拆分。
7. 对应的 capability/deployment 回滚窗口内，旧 Catalog、Milvus collection、Wiki 目录和原始导入文件只读保留；Platform 从 Package 权威输入完成重建验证且该窗口正式关闭后，才允许清理。

建议拆库步骤：

```text
A. 表级 inventory + FK/调用方/迁移版本清单
     ↓
B. 新建独立 HarnessBase / KnowledgeBaseMetadata
     ↓
C. Repository 改为显式接收各自 SessionFactory
     ↓
D. 在原数据库内验证两套 migration metadata 可独立建库
     ↓
E. 在生产数据副本演练 freeze/drain/copy/verify/rollback
     ↓
F. 维护窗口停止相关写入并生成一致性快照
     ↓
G. 复制到目标库、校验后通过单一配置版本原子切换
     ↓
H. 旧源只读保留，持续执行回滚演练与 Package 重建验证
     ↓
I. capability/deployment 回滚窗口关闭后，按授权清理旧 Platform 表、索引和冗余文件
```

迁移校验至少包括：逐表行数、主键集合、规范化内容摘要、外键完整性、Job lease 状态、文件/对象存储引用可达性，以及数据库 secret redaction。原子切换必须由一个可审计的 deployment/config revision 同时选择 Catalog、Blob、Vector Index 和 Wiki root，禁止依靠多个进程分别修改环境变量完成非原子切换。

这里的“原子”只指 **active revision 指针** 的比较交换，不表示 PostgreSQL、对象存储、Milvus 和文件系统之间存在跨存储事务。切换前必须先发布完整且已校验的新 revision；单次请求在入口解析并固定一个 revision。切换传播或部分失败时，并发请求可以分别落在旧、新两个完整 revision，但单次请求不得静默拼接不同 revision 的 Catalog/Blob/Index。新 revision 任一依赖未就绪时，读路径回退到只读旧 revision 或返回可重试错误，后台 reconciliation 完成后再推进指针；不得破坏旧源以强行完成切换。

现有 `catalog_migration.py` 的 drain、事务导出和摘要校验思路可以复用，但必须拆成两个所有者明确的迁移器。

### 10.2 当前依赖不是单向，而是双向依赖网

目标依赖图是单向的，但当前源码实际为：

```text
graph/harness
  ├─→ analytics.models / semantic assets / nl2sql schemas
  ├─→ knowledge paths / jobs / models
  └─→ tools by concrete tool name

tools
  ├─→ knowledge / analytics
  ├─→ graph registries / session manager / citations / attachment store
  └─→ LangChain ToolRuntime / LangGraph interrupt

knowledge
  ├─→ tools.llm_wiki_tools      # compiler agent
  ├─→ analytics                 # import/entity/semantic flows
  ├─→ graph through tool layer
  ├─← runtime_control           # 反向复用 knowledge.queue_repository 的 DB lease 表达式
  └─→ global config/runtime paths

analytics
  ├─→ knowledge database/catalog models
  ├─→ graph evidence/session registries
  ├─→ tools span/citation helpers
  └─→ global config/runtime paths
```

拆环顺序必须先于目录移动：

1. 在独立 `knowledge_contracts` 中建立无框架 DTO、错误、Evidence、Principal、Correlation、Job 和 Query Plan 契约。
2. 将 graph 中的数据库 Evidence、Schema Evidence、SQL revision registry 改为 Platform Repository/Service；Harness 不再拥有这些状态。
3. 将 citation 正规化、TraceSink、Attachment/Blob read 抽成端口；Platform 不 import `graph.citations` 或 `graph.attachment_store`。
4. 将 Wiki Compiler 改为直接调用 Platform application services，而不是实例化 `tools.llm_wiki_tools`。
5. 将 Knowledge import 对 Analytics 的调用改为发布 Platform domain command/event，避免子域间反向 import。
6. 最后让目标 Harness 路径不再直接引用 Analytics/Knowledge Model、Middleware 和协议字段；PuddingClaw legacy 路径保留但停止默认使用。

`backend/knowledge/queue_repository.py` 不能整体划归 Platform。它混合了两层能力：

- 通用的数据库权威时间、lease expiry/bind params、CAS claim/heartbeat/fencing 原语；
- Knowledge Import、Semantic Dimension 等业务 Queue 对这些原语的使用。

`backend/runtime_control.py` 和 Backend lease 语义直接依赖其中的通用原语。目标处置是：

```text
knowledge.queue_repository
  ├─ generic DB lease primitives
  │    ├─→ Harness 内部独立实现，或版本锁定的极小 shared library
  │    └─→ Platform 内部独立实现，或同一 shared library
  └─ knowledge job claim/process helpers
       └─→ Platform Jobs
```

优先建议发布一个无 ORM Model、无 Claw/Knowledge import、只包含 dialect-aware lease 算法与契约测试的极小 library；如果共享发布和升级成本过高，则两边各自复制实现并共享黑盒一致性测试。禁止 Platform 在拆分后继续从 Claw `runtime_control` 或 `knowledge.queue_repository` 回调 lease 原语。

依赖门禁不只检查 `knowledge_platform/domain`，还必须覆盖整个目标服务包：

```text
knowledge_platform/** ─X→ backend/graph/**
knowledge_platform/** ─X→ backend/harness/**
knowledge_platform/** ─X→ backend/tools/**
knowledge_platform/application/** ─X→ langchain_core.tools / langgraph
claw_harness/** ─X→ backend/knowledge/** / backend/analytics/** / backend/vanna/**
```

### 10.3 `deepagents_manager.py` 是当前业务接线中心

`backend/graph/deepagents_manager.py` 目前根据 Tool name 字符串注入 `session_id`、`query_id`、`run_id`、attachment、当前消息和会话文档，并负责挂载 `/knowledge`、`/semantic-assets`、`/sql-guardrails`、`/analytics-models`。这是拆分的核心接线面，不能只修改 Tool 文件。

迁移时按 Capability 建立接线消除清单：

1. 外部 Platform MCP Tool 由通用 MCP discovery 创建，Claw 不按 Tool name 注入业务字段。
2. 调用者身份由 MCP/HTTP Authorization 建立；Claw correlation ID 只能作为不透明 `_meta`，不得成为授权事实源。
3. 当前消息、附件和会话文档若需进入 Platform，必须由 Agent 显式调用外部 Asset ingestion/admin capability；Query Tool 不隐式读取 Claw Session。
4. 业务虚拟路径全部移除。外部知识通过 MCP Resource URI 或 Workspace 文件访问。
5. 每迁出一个 Capability，都要从 tool-name injection、Toolset、Intent Router、Prompt、Middleware、virtual mount 和 interrupt resolver 七个位置同时清除。

### 10.4 运行协议字段也属于拆分范围

`analytics_model_id` 已进入 Agent、Session、Evaluation、Headless、Prompt、Delegation 和 Harness deterministic checks。仅做到“Claw 不 import analytics”仍不算完成。

目标处理：

- PuddingHarness Agent/Session API 不包含 `analytics_model_id`；
- PuddingHarness State、Run record、TaskProfile、Delegation contract 不包含分析专属字段；
- 新 evaluation protocol version 不包含该字段；PuddingClaw `protocol-1.0` 和旧结果只读兼容；
- PuddingHarness `prompts/AGENTS.md` 不包含 Analytics Model 和知识查询专属说明；
- `harness/analytics_invariants.py` 迁入 Platform Evaluation，Harness deterministic checks 不再解释业务指标不变量；
- PuddingHarness Headless Worker CLI 不包含 analytics model routing；如果调用外部 Platform，由任务输入携带普通 MCP server 配置或 Resource URI；
- 旧字段在兼容窗口内映射为外部 `collection_id`/Resource URI 时必须由 Application Adapter 完成，不能留在 Harness Core。

协议清理需采用新版本而不是静默改变 `protocol-1.0` 含义，并提供旧 Session/Evaluation artifact 的只读迁移说明。

### 10.5 推荐的迁移中间态

最终态坚持独立 Platform + 外挂 MCP，但不直接从当前循环依赖跳到拆仓或删除代码。迁移顺序是不可交换的架构约束：

```text
Stage 0：Phase 0A/0B/0C inventory、契约、测试、拆库演练和依赖门禁
          生产代码、调用路径和运行数据零行为变化
                         ↓
Stage A1：同仓库建立 knowledge_platform 分层包并逐 Capability 抽取
          旧实现、旧 Tool 和旧生产调用链原样保留
                         ↓
Stage A2：同仓库、同进程 shadow
          新链路只做对照，legacy 仍是唯一生产服务路径
                         ↓
Stage B：同仓库、独立进程/sidecar
          REST/MCP 成为唯一跨边界接口，按 Capability 灰度切流并可回退
                         ↓
Stage C1：全部 Capability 稳定后抽取 `puddingknowledge` 和 `puddingharness` RC
          Platform 独立发行；Harness RC 完成 E2E/升级/回退演练；
          PuddingClaw 仓库、默认发行和 legacy path 完整保留
                         ↓
Stage C2：Platform 与 Harness RC 稳定且 release rollback window 关闭后
          将已验证 RC 提升为 `puddingharness` 正式发行；
          PuddingClaw 转入维护/归档模式，不删除任何代码
```

一句话总纲是：**原仓库内解耦 → 同进程 shadow → 独立进程灰度 → 抽取两个新仓库并验证 RC → 关闭 release rollback window → 新仓库正式首发、旧仓库归档**。拆仓是边界已经成立后的机械操作，归档是终点，不得用提前拆仓或停发 PuddingClaw 倒逼解耦。

本文区分三类回滚窗口：`capability rollback window` 属于 Phase 8 单项流量开关；`release rollback window` 属于 Phase 10/11 产品默认发行切换；`installation rollback window` 属于 11.20 中每个用户安装的有状态迁移，从该安装进入 CUTOVER 开始，到用户确认 FINALIZED 结束。关闭 release rollback window 不会关闭尚未开始或仍在进行的 installation rollback window；PuddingClaw 归档发行物必须继续支持后者。

Stage A 不是恢复旧方案的 Harness Extension SPI：Platform application service 不向 Harness 注册业务 Middleware/Prompt/状态；保留的旧 Tool 只用于对照测试和回滚。新功能只能进入 Platform API，不再进入旧 DeepAgents Tool。对每个 Capability，都必须先完成 A1 抽取和等价测试，再进入 A2 shadow，不能以 shadow 调用尚未隔离依赖的旧业务实现冒充 Platform。

## 11. 现有源码迁移映射

### 11.0 范围基线与完整子域清单

v0.1 的文件举例不能作为工作量清单。当前规模基线约为：

- `backend/knowledge/`：40 个 Python 文件，约 1.9 万行；
- `backend/analytics/`：约 1.84 万行；
- `backend/tools/database/`：17 个 Python 文件（含 `__init__.py`），数据库 Tool 主实现集中在该子包；
- `backend/tools/llm_wiki_tools.py`：647 行、9 个 Tool；
- `backend/analytics/table_catalog.py`：1433 行；
- `backend/vanna/`：vendored fork，不是普通 pip dependency；
- 另外还有 graph/harness/api/evaluation/frontend/electron/deploy-cli/docker-compose/config/llm provider 等跨域调用方。

迁移 inventory 必须覆盖以下子域，而不是只覆盖查询 Tool：

| 子域 | 当前主要位置 | 目标归属 |
| --- | --- | --- |
| Knowledge Catalog/Files | `knowledge/models.py`、`service.py`、`sources.py`、`paths.py` | Platform Catalog/Storage |
| Parser Registry | `knowledge/parsers/*`、`mineru_client.py` | Platform Processing |
| Import/Publish Queue | `knowledge/import_jobs.py`、`import_worker.py`、`queue_repository.py` 的 Knowledge 业务层 | Platform Jobs；通用 lease 原语按 10.2 拆分，不整体迁移 |
| Local/Multimodal Index | `knowledge/indexer.py`、`tools/search_knowledge_tool.py` | Platform Retrieval |
| Portal Cross-source Search | `knowledge/portal_search.py` | Platform Catalog Search |
| Read Later/Web Capture | `knowledge/read_later.py`、`api/read_later.py` | Platform Connector/Ingestion |
| Feishu Connectors | `knowledge/connectors/*`、`api/feishu_connector.py` | Platform Connectors |
| Wiki | `llm_wiki*.py`、`brain_schema.py`、`gbrain_runtime.py` | Platform Wiki/Processing |
| Semantic Dimension Jobs | `knowledge/semantic_dimension_*`、相关 Tools/Graph resume | Platform Semantic Authoring Jobs |
| Table Catalog/Logical Dataset | `analytics/table_catalog.py`、logical dataset Tools | Platform Structured Assets |
| Semantic Assets/Runtime | `analytics/semantic_assets/*`、`semantic_runtime/*` | Platform Semantic Domain |
| Semantic Authoring | `analytics/semantic_authoring/*`、semantic steward Tools | Platform Admin/Processing |
| Analytics Collection Compatibility | `analytics/models/*` | Platform Collection compiler/importer |
| NL2SQL/Vanna | `analytics/nl2sql/*`、`vanna/*`、database Tools | Platform Database Query |
| Package Export | `analytics/project_export/*` | Platform Package/Workspace |
| Query Results/Evidence | result store、graph citations/evidence | Platform Query Result/Evidence contract |
| Knowledge/Analytics Skills | `backend/skills/*` 中 10 个知识耦合 Skill、`skills/puddingclaw/SKILL.md` | Platform query/admin Skills、通用骨架去耦、Workspace 适配或退役 |
| Deep Research | `tools/deep_research_tool.py` | Claw 通用能力，解除对 Knowledge tool category 的隐式依赖 |
| Intent Router | `graph/middlewares/tool_intent_router.py` | 不进入 `puddingharness`；PuddingClaw legacy 实现停止默认使用，不保留 web-search 通用壳的目标态副本 |
| Config/Credentials | `config.py`、Provider Registry、LocalCredentialStore、各子域 config getter | 逐 key/credential entry 拆分所有权 |
| Knowledge UI | frontend knowledge/analytics routes | Platform Console |
| Generic evidence rendering | `SourcesPanel` 等 | Claw 通用 renderer + Platform Console shared contract |
| Distribution/Infra | Electron、Deploy CLI、Compose、scripts | 分拆后的 Claw/Platform distribution |

每个子域在 Phase 0 inventory 中必须列出：源码、ORM 表、文件目录、后台任务、**每个 config key 和 credential entry**、API、UI、Tool、Skill、Prompt、Middleware、测试、可选依赖、基础设施、密钥和数据迁移方式。配置/凭据清单必须标注当前读写者、目标所有者、迁移转换、secret backend、轮换方式和旧配置删除版本，不能只记录配置文件归属。

### 11.1 Catalog 与导入 Pipeline

现有资产：

- `backend/knowledge/models.py`：KnowledgeBase、Document、Source Connection、Database Source、Table Asset；
- `backend/knowledge/service.py`：Markdown、PDF、Excel、CSV、TSV、TXT、DOCX 导入；
- `backend/knowledge/parsers/`：Parser Registry、ParseResult、Markdown/Structured Blocks/Assets；
- `backend/knowledge/import_jobs.py`、`import_worker.py`：异步导入和发布任务。

迁移目标：

- 归入 Platform Catalog、Ingestion 和 Processing Pipeline；
- 将 Catalog 中与 Claw Session/Headless Worker 混合的模型拆开；
- 将物理路径转换为 Storage Port 和稳定 Asset URI；
- 保留 content digest、source revision、deduplication 和 job checkpoint。

### 11.2 LlamaIndex/Milvus RAG

当前 `backend/tools/search_knowledge_tool.py` 的 `LlamaIndexKnowledgeQueryTool` 同时拥有 Tool schema 和大量业务实现，包括索引加载、多模态召回、BM25、RRF、rerank、score 标准化、citation 和 trace。

迁移目标：

```text
LlamaIndexKnowledgeQueryTool
  ├─ schema/description          → MCP Adapter schema
  ├─ retrieval/fusion/rerank     → DocumentRetrievalService
  ├─ index loading               → LlamaIndex/Milvus Infrastructure Adapter
  ├─ citation normalization      → unified Evidence mapper
  └─ graph trace                 → Platform TraceSink
```

该 Tool 不进入 `puddingharness`，且不得在其中保留同名远程代理 Tool；PuddingClaw legacy 源码继续保留，但迁移切流后不再承接默认流量。

### 11.3 LLM Wiki

当前 `backend/knowledge/llm_wiki.py` 已有独立 `LlmWikiService.query()`，同时承担 Raw snapshot、publish、lint、retire、workspace migration 和 GBrain 集成；但 **Raw → Wiki 页面内容生成不在该 Service 内**，真正的编译 Agent 位于 `backend/knowledge/llm_wiki_compiler_agent.py`，它通过 LangChain `create_agent` 实例化 `tools.llm_wiki_tools` 中的 context/publish/lint Tool，再回调 `LlmWikiService.publish()`。

拆分前最先要解决的是三方依赖环：

```text
knowledge.llm_wiki_compiler_agent
      → tools.llm_wiki_tools
            → graph.attachment_store / graph.citations
            → knowledge.import_jobs / llm_wiki / service
```

目标改为 Platform Worker 直接依赖 application ports：

```text
WikiCompilationWorker
├─ RawSnapshotRepository
├─ WikiContextService
├─ ModelGateway
├─ WikiDraftValidator
├─ WikiPublishingService
└─ PlatformJobRepository
```

Worker 可以使用 LangChain 或其他 Agent SDK 作为内部实现，但不实例化公共 MCP Tool，也不依赖 Claw attachment、citation、Session 或 graph registry。

迁移目标：

```text
WikiQueryService       # 在线只读查询
WikiCompilerService    # AI-native Raw → Wiki 处理
WikiPublishingService  # lint/publish/retire
WikiEmbeddingProvider
GBrainProvider         # 可选
```

`backend/tools/llm_wiki_tools.py` 不只是 Query Adapter，而是包含 context、publish、lint、query、compile、conversation documents、create raw、start ingest、retire 等 9 个 Tool。其处置如下：

| 当前 Tool 类别 | v1 处置 |
| --- | --- |
| `LlmWikiQueryTool` | 迁为 Platform MCP `wiki_query` |
| context/query read path | 迁为 Platform application service/Resource |
| publish/lint/compile/retire | 不向普通外部 Agent 暴露；迁为 Admin API 或内部 Worker command |
| conversation documents/create raw/start ingest | 解除 Claw Session/Attachment 绑定后，后续映射为受控 ingestion API；不进入 Query Plane |

v1 只要求迁移 Wiki Query。编译链后续以 Platform 内部 Worker 身份迁移，不阻塞只读服务上线；9 个 Wiki Tool 均不进入 `puddingharness`，PuddingClaw 中的 legacy 实现保留但在最终切换后停止默认使用。

### 11.4 Excel/Pandas Query

当前 `backend/tools/pandas_knowledge_tool.py` 同时处理：

- ToolRuntime 和 Agent state；
- Table Asset Catalog；
- 文件/Sheet 匹配；
- DataFrame 加载；
- Semantic Context；
- Pandas Query Engine；
- 结果格式化。

补充事实：`KnowledgeTableAsset` ORM 表位于 `knowledge.models`，但负责扫描、Profile、逻辑拼接数据集和 Catalog 维护的 `TableAssetCatalog` 实现在 `backend/analytics/table_catalog.py`。它是跨 Knowledge Storage 与 Analytics Query 的桥接模块，不能按当前目录名直接整体归属。

迁移目标：

```text
TableQueryService
├─ TableAssetRepository
├─ TableSelector
├─ SemanticContextProvider
├─ DataFrame/DuckDB Execution Provider
├─ ExpressionPolicy
└─ QueryResultRepository
```

公共 Capability 使用 `table_query`，不暴露 `PandasKnowledgeQueryTool` 或 LlamaIndex PandasQueryEngine 类型。

### 11.5 Vanna/NL2SQL/数据库执行

可复用核心：

- `backend/analytics/nl2sql/service.py`：完整 grounded SQL pipeline；
- `backend/analytics/nl2sql/runtime.py`：Vanna Provider 构建；
- `backend/analytics/nl2sql/table_router.py`：数据库表路由；
- `backend/analytics/nl2sql/sql_runner.py`：只读 SQL 校验和执行；
- `backend/analytics/nl2sql/guardrails.py`：业务 Guardrail；
- `backend/analytics/semantic_runtime/`：统一语义上下文。

必须移除的反向依赖：

- `analytics.nl2sql.evidence_service` 对 `graph.database_evidence`、`graph.database_schema_evidence` 和 Session Manager 的依赖；
- SQL generate/validate/execute Tool 对 ToolRuntime、LangGraph interrupt、Run/Goal 和 receipt registry 的依赖；
- result store 对 Claw Session Manager 和 PuddingClawPaths 的依赖；
- Vanna、Guardrail、数据库源对全局 Claw config 和路径单例的依赖。

数据库 Agent Tools 的主实现位于 `backend/tools/database/` 子包，包含 evidence、schema inspect、semantic entity lookup、SQL generate/validate/execute、result page/source、trace inspect、scope、formatting 和兼容 Tool。`backend/tools/database_knowledge_tool.py` 只是兼容 re-export shim；迁移和删除清单必须以整个子包为单位核对。

`backend/vanna/` 是 vendored fork，不是“从 Claw requirements 删除一个 pip 包”即可完成。它包含本地 Milvus entity collection、`entity_type`、canonical name、alias 和 table-column 等定制。Platform 拆分必须单独形成 fork 决策：

1. 记录当前上游基线 commit/version 和全部本地补丁；
2. 为 entity 检索行为建立黑盒兼容测试；
3. 在“继续维护独立 fork”和“将扩展点贡献/适配到上游”之间做显式选择；
4. 将选定源码和许可证材料迁入 Platform 仓库或依赖构建；
5. `puddingharness` 不包含 vendored Vanna 源码及其运行依赖；不能只从 requirements 排除包而保留可达源码。PuddingClaw 中的原实现保持不变。

迁移目标：

```text
DatabaseQueryService
├─ DatasetResolver
├─ SemanticContextCompiler
├─ DatabaseRouter
├─ VannaEvidenceProvider
├─ SqlGenerator
├─ SqlValidator
├─ GuardrailEvaluator
├─ ReadonlyExecutor
├─ QueryPlanRepository
└─ QueryResultRepository
```

Harness receipt 被 Platform `query_plan_id`、调用者 Principal、TTL、SQL hash、Dataset Version 和 Deployment Revision 取代。

### 11.6 Semantic Assets 与当前 Analytics Model

- `backend/analytics/semantic_assets/` 迁为通用 Semantic Asset Registry；
- `backend/analytics/semantic_runtime/` 保留为结构化 Query Capability 的共享编译器；
- `backend/analytics/models/` 迁为 `relational-analytics` Dataset compiler/compatibility importer；
- `AnalyticsModel` API 在迁移期保留兼容读取，最终由结构化 Knowledge Dataset API 取代。

### 11.7 Package/Workspace 导出

`backend/analytics/project_export/` 当前已能生成带 manifest、AGENTS.md、bindings、profiles、Guardrails、校验和和测试的分析项目 ZIP。

迁移目标：

- 将其泛化为 `KnowledgePackageBuilder` 和 `WorkspaceMaterializer`；
- 当前 Analysis Project 成为 `relational-analytics` 结构化 Dataset 的一个编译 target；
- 增加 Document、Wiki、Excel、Database、Semantic 和 Evidence 的通用 manifest；
- 增加索引重建信息、Package schema version 和 importer compatibility checks。

### 11.8 不进入 `puddingharness` 的业务内容

> 本节描述目标仓库的排除边界，而不是 PuddingClaw 的删除清单：这些内容**不进入 `puddingharness`**，并在 PuddingClaw 迁移期灰度中停止默认流量；PuddingClaw 仓库本身永久保留全部实现。

`puddingharness` 首发时必须排除的内容包括：

- Knowledge/Analytics Tool 定义和 Toolset；
- Knowledge Tool intent routing；
- Analytics Model 和 Semantic Asset Middleware；
- `/knowledge`、`/semantic-assets`、`/sql-guardrails`、`/analytics-models` 等业务虚拟挂载及 `graph/virtual_paths.py` 中的保留路径；
- Knowledge-specific stable prompt/tool guides；
- `backend/tools/database/` 全子包以及兼容 re-export shim；
- `semantic_steward_tool` 的 HMAC discovery/plan receipt；
- `request_dimension_build_rule_tool`、`request_logical_dataset_rule_tool` 及其 LangGraph interrupt/resume registry；
- Vanna vendored fork、LlamaIndex、Wiki、Pandas Query、SQL Guardrail 和查询结果 Worker；
- Knowledge/Analytics API Router；
- Knowledge Catalog lifecycle；
- Knowledge-specific interruption/receipt/revision state。

PuddingHarness Core 只包含通用 MCP Client/Server discovery、Tool 调用、授权呈现、结果渲染和外部 Resource 处理能力。

### 11.9 Portal Search、Read Later 与 Connectors

v0.1 漏掉了 Knowledge Query 之外的数据入口和统一搜索面：

- `knowledge/portal_search.py` 提供跨 Catalog 的搜索、建议和文件 watcher；
- `knowledge/read_later.py` 与 `api/read_later.py` 提供 URL 抓取、重试、入库和 Wiki promotion；
- `knowledge/connectors/*`、`api/knowledge_sources.py`、`api/feishu_connector.py` 提供飞书 Wiki/多维表格发现、同步、授权和增量状态。

这些全部归入 Platform：

```text
Portal Search       → CatalogSearchService / knowledge_search metadata channel
Read Later          → WebCaptureConnector + IngestionJob
Feishu connectors   → Connector SPI + SourceSyncJob
Catalog watcher     → Platform lifecycle worker
```

Connector credential 必须进入 Platform Vault/Secret Provider。Claw 不保存飞书 Knowledge Source token，也不运行这些同步 Worker。Connector 的实时查询能力若需要对 Agent 开放，应作为 Platform Capability 或独立外部 MCP Server 聚合，不写入 Claw Toolset。

### 11.10 Semantic Authoring、Dimension Build 与 Logical Dataset

`backend/analytics/semantic_authoring/` 约 1676 行，当前负责语义定义 discovery、Markdown 生成计划、校验和发布；此外还有：

- `knowledge/semantic_dimension_crosswalk.py`；
- `semantic_dimension_jobs.py`、publisher、rule contract、worker；
- `tools/semantic_steward_tool.py`；
- `tools/semantic_dimension_build_tool.py`；
- `request_dimension_build_rule_tool.py`；
- `logical_dataset_tools.py`、`request_logical_dataset_rule_tool.py`；
- `graph/dimension_build_resume.py`、`graph/logical_dataset_resume.py`。

这些属于 Platform Admin/Processing Plane，不属于 Claw Harness。迁移原则：

1. discovery、prepare、validate、publish 成为 Platform application commands；
2. HMAC/plan receipt 改为 Platform Draft/Publish Plan，由 Principal、TTL、baseline hash 和 target revision 约束；
3. interrupt 型业务确认改为 Admin API 的 `waiting_for_decision` Job 状态或外部 Agent 可选的 MCP elicitation，不依赖 LangGraph interrupt；
4. Semantic Dimension 和 Logical Dataset 的 Job/Rule/Decision 全部存入 Platform Catalog；
5. 第一版 Query Plane 不暴露这些写能力，Platform Console 使用 Admin API；
6. Claw 如果被用户用于执行维护任务，只能像其他外部 Agent 一样调用显式安装的 Platform Admin MCP，默认连接不授予写 scope。

### 11.11 Agent、Session、Headless 与 Evaluation 协议

需要迁移或版本化的调用面包括：

- `api/agent.py`、`api/sessions.py` 的 `analytics_model_id`；
- `graph/session_manager.py`、DeepAgentState、delegation control 的分析字段；
- `harness/models.py`、`task_profiles.py`、`coordinators.py` 的分析上下文；
- `harness/analytics_invariants.py` 及 `deterministic_checks.py` 的业务验收；
- `evaluation/contracts.py`、candidate、runner、worker manager 和 `protocol-1.0.json`；
- Deploy CLI Headless worker 命令中的 `analytics_model_id` 和 analytics routing。

处置：

```text
analytics_model_id
  ├─ Harness protocol         → 删除
  ├─ Platform compatibility   → 映射到 collection/resource URI
  └─ Historical artifacts     → 只读兼容

analytics invariants
  ├─ Harness deterministic checks → 删除业务解释
  └─ Platform evaluation          → 迁入 Collection evaluation
```

Evaluation 域本身不是 Knowledge 业务，应保留为通用 Harness 评测框架；只迁出分析模型专属 schema、fixture、rubric compiler hook 和 invariant evaluator。Platform 另有自己的 Retrieval/Query/Dataset Evaluation，两者通过普通外部评测 Adapter 对接，不能整域搬迁。

### 11.12 Frontend 所有权

当前 frontend API client 中存在大量 Knowledge/Analytics endpoint 封装，并有 Knowledge Library、Imports、Sources、Search、Read Later、Schema、Settings、Analytics 等页面。拆服务后目标所有权为：

| UI | 目标归属 |
| --- | --- |
| Knowledge Library、Upload、Import Jobs | Platform Console |
| Sources/Connector/OAuth | Platform Console |
| Search、Wiki、Schema/Semantic Authoring | Platform Console |
| Analytics Collection、Query Results、Guardrails | Platform Console |
| Platform Deployment、Index、Provider 配置 | Platform Console |
| Claw Chat/Task/Session/Workspace | Claw UI |
| 通用 MCP Server 管理 | Claw UI |
| 通用 Tool result、Resource、Evidence/Citation renderer | Claw UI 保留框架无关版本 |

`SourcesPanel` 可以留在 Claw，但必须只消费通用 MCP ContentBlock/ResourceLink 或共享 Evidence JSON Schema，不能依赖 `graph/citations.py` 的 Knowledge 私有编码。Platform Console 可以复用同一前端 schema/client package。

迁移期可以由 Claw frontend 通过 Platform BFF/proxy 保持旧路由，但需满足：

- 页面请求实际到达 Platform API；
- Platform auth/session 独立；
- 新 Platform 页面不继续写入 Claw `frontend/src/lib/api.ts` 单体客户端；
- 最终 Claw distribution 不打包 Platform Console route。

### 11.13 Electron、Deploy CLI、Extensions 与基础设施

当前分发层与业务能力强耦合：Electron 直接启动/停止 PostgreSQL 和 Milvus；Deploy CLI 暴露 `harness/knowledge/analytics/full` profile；Backend 使用 `PUDDINGCLAW_EXTENSIONS` 和 HTTP route gating；`docker-compose.infra.yml` 同时承载 PostgreSQL/pgvector、Milvus、etcd、MinIO。

目标拆分：

```text
Claw Distribution
├─ Pure Harness runtime
├─ generic sandbox/runtime dependencies
├─ MCP client configuration
└─ no Knowledge infrastructure lifecycle

Knowledge Platform Distribution
├─ API/MCP/Worker/Console
├─ Platform Catalog DB
├─ Milvus + etcd + MinIO
├─ optional PostgreSQL/pgvector
├─ optional MinerU/parser workers
└─ platform deploy CLI / compose / helm
```

具体处置：

1. Electron `docker.js` 不再把 Milvus 作为 Claw 基础设施；本地一体化产品若仍需一键启动，应调用独立 Platform supervisor/installer，而不是由 Harness manager 拥有容器。
2. `docker-compose.infra.yml` 拆为 Claw 通用依赖与 Platform stack；Milvus、etcd、MinIO、Knowledge PostgreSQL/pgvector 归 Platform。
3. `scripts/start-local-infra.sh` 中 Knowledge/pgvector/Milvus 安装逻辑迁到 Platform distribution。
4. Claw Deploy CLI 最终只保留 Harness profile；`knowledge/analytics/full` 转为迁移别名或组合安装 recipe，不再映射 Claw extension flags。
5. 新 Platform CLI 独立提供 `init/deploy/status/migrate/import/export/index`。
6. `backend/extensions.py` 的 knowledge/analytics 开关不进入 `puddingharness`；PuddingClaw 中仅作为 legacy path kill switch 保留，不能控制 Platform 能力。
7. Claw Health 不探测 MinerU、Milvus、pgvector、Vanna 或业务数据库，只报告外部 MCP endpoint 的通用连接状态。

### 11.14 gbrain

gbrain 当前既由 `knowledge/brain_schema.py` 和 `gbrain_runtime.py` 管理 schema/runtime，又在 `mcp_clients/servers.py` 中作为内置 stdio MCP Server 注册。它是“外部 MCP 能力”可行性的现有先例，但当前配置、schema pack、PostgreSQL 和 Wiki 归属仍与 Claw 混合。

目标决定：

1. Pudding 维护的 gbrain schema pack、runtime home、AI provider mapping、PostgreSQL 初始化、Wiki import/retire 全部归 Platform Wiki/GBrain Provider。
2. hard-coded `gbrain` server registry、过滤白名单、readiness gate 和 Pudding Wiki 专用配置不进入 `puddingharness`；PuddingClaw 中保留 legacy 实现。
3. 如果用户独立安装 gbrain MCP，仍可像任意第三方 MCP 一样手工连接 Claw；Claw 不识别其 Pudding 专属语义，也不自动配置。
4. Platform 可以选择内部 CLI/subprocess Adapter 或内部 MCP Client 调用 gbrain，但这属于 Platform infrastructure decision。
5. gbrain 不成为 Knowledge Platform 的必选存储；Markdown Wiki 仍为可迁移事实源，gbrain/pgvector 是可重建投影。

### 11.15 Embedding、Rerank 与 Model Gateway

`backend/llm/` 当前同时服务 Harness 模型调用和 Knowledge 的 embedding、multimodal embedding、rerank。不能整目录搬迁。

拆分为：

- Harness ModelClient/Agent model binding 留在 Claw；
- text embedding、multimodal embedding、rerank client 及其 provider config 迁入 Platform；
- 通用 provider registry 如需共享，发布为语言/进程无关配置 schema 或独立小型 library，不允许 Platform 回调 Claw 获取密钥；
- Platform Deployment 独立保存 provider binding 和 secret reference；
- Package 仅记录生成索引时的 provider/model/version，不包含凭据。

### 11.16 Evaluation 的保留边界

Claw Evaluation 保留：通用 Agent trajectory、Tool protocol、task outcome、latency/token、rubric 和回归框架。

Platform Evaluation 新增：

- retrieval relevance/recall；
- citation/locator correctness；
- Wiki query coverage；
- table result invariant；
- NL2SQL execution accuracy、semantic correctness 和安全拒绝；
- Package import/rebuild reproducibility；
- Collection permission negative cases。

跨产品 E2E 通过外部 MCP 黑盒场景验证，不允许 Claw Evaluation import Platform Python 模块。Claw 只观察公开 Tool schema、调用、Resource/Evidence 和最终任务结果。

### 11.17 Knowledge/Analytics Skills 与外部委托 Skill

Phase 0A 必须逐个盘点以下业务 Skill 及其 references/scripts/assets：

```text
backend/skills/knowledge-search/
backend/skills/llm-wiki/
backend/skills/semantic-steward/
backend/skills/database-analysis/
backend/skills/table-analysis/
backend/skills/build-semantic-dimension/
backend/skills/build-logical-dataset/
backend/skills/sql-guardrail-designer/
backend/skills/hv-analysis/
backend/skills/github-monitor/
skills/puddingclaw/
```

目标处置：

| Skill | 目标 |
| --- | --- |
| knowledge-search、llm-wiki、database-analysis、table-analysis | 重写为 Platform MCP/Workspace 查询 Skill，不再引用 Claw Toolset、虚拟路径、Session 或 receipt |
| semantic-steward、build-semantic-dimension、build-logical-dataset、sql-guardrail-designer | 迁入 Platform Admin Skill bundle；仅在显式授予管理 scope 时安装/发现 |
| hv-analysis | 保留为通用研究方法 Skill；删除或替换其对内置 Knowledge Tool 的任何隐式假设，不能随 Knowledge Toolset 被误删 |
| github-monitor | 保留 GitHub 监控与 Markdown 生成骨架；`/knowledge/` 默认输出改为调用者工作区或显式 Asset ingestion/output Adapter，删除 `llamaindex_knowledge_query` 和内置虚拟挂载假设 |
| `skills/puddingclaw/SKILL.md` | 当前“把企业数据任务委托给 PuddingClaw Worker”的契约退役；若仍需外部委托，重写为直接连接 Knowledge Platform MCP/CLI，不再经过 Claw Session/Analytics Model |

`github-monitor` 的耦合不仅在说明文本：`backend/skills/github-monitor/SKILL.md` 硬编码 `/knowledge/` 写入和 `llamaindex_knowledge_query`，`scripts/store_kb.py` 也将 `/knowledge` 设为默认目标。迁移时必须同时修改 Skill、scripts 和 validation；只改说明会保留运行时耦合。

Claw 的 pure Harness 发行物不内置 Platform 查询/管理 Skill。用户连接 Platform MCP 时，使用 MCP Tool description、Prompts，或单独安装 Platform 发布的 Skill bundle。Workspace materializer 可以把同源 Skill 模板编译进导出目录。

### 11.18 Deep Research 与 Intent Router

`backend/tools/deep_research_tool.py` 属于通用 Harness 研究/子 Agent 能力，但当前通过 `get_tools_by_categories({"knowledge"})` 获取 `llamaindex_knowledge_query`。删除 Knowledge Toolset 后若不处理，它会静默丢失本地知识检索能力。

目标选择：保留 Deep Research 通用骨架，但彻底移除 Knowledge category 依赖：

- 子 Agent 只继承当前 Harness 已发现且调用者授权的通用/MCP read capabilities；
- 不硬编码 `llamaindex_knowledge_query` 或任何 Platform Tool 名；
- 若研究 scope 包含外部 Resource URI，由通用 MCP Resource/Tool delegation contract 显式传入；
- Tool 不因未连接 Knowledge Platform 而退化为与描述不一致的状态，运行前应返回实际 capability inventory；
- 为“无 Knowledge MCP”和“连接任意第三方 Knowledge MCP”建立测试。

`ToolIntentRouterMiddleware` 当前包含 semantic dimension、knowledge catalog、database、table、knowledge RAG 和 web search 六类软路由。目标是 **整个 Middleware 不进入 `puddingharness`**，不保留只含 web search 的通用壳；PuddingClaw legacy 源码保持不变。理由：

1. Knowledge/Analytics 路由属于 Platform `knowledge_query` 和 MCP Tool description；
2. Web Search MCP/Tool 也应通过自身描述和 Skill 发现，不应继续由 Claw 关键词表硬编码；
3. 保留空壳会延续 Harness 对具体能力名称的所有权。

如果未来 Harness 需要通用能力选择优化，应另建与具体业务/tool name 无关、基于标准 capability metadata 的机制，不复用当前 Intent Router。

### 11.19 Config 与 Credential Ownership

Platform 当前通过全局 `config.py` 和 `LocalCredentialStore` 读取/写入大量配置。除 embedding/rerank 和 connector token 外，还包括 Parser Registry、MinerU、LlamaParse、Knowledge root、Portal Search、Wiki retrieval/compiler、Milvus、Vanna、Database QA、SQL result、gbrain 等。

典型隐性写路径是 `knowledge/parsers/registry.py`：它直接调用 `load_config/save_config`，并通过 `LocalCredentialStore` 创建或解析 MinerU/LlamaParse credential。仅搬 Parser 实现会继续修改 Claw Home。

Phase 0A 产出 `config-credential-ownership.yaml`，每项至少包含：

```yaml
- key: knowledge.parsers.items.mineru_cloud_precise
  current_readers:
    - knowledge/parsers/registry.py
  current_writers:
    - knowledge/parsers/registry.py
    - api/knowledge.py
  secret_refs:
    - MINERU_API_TOKEN
  target_owner: knowledge-platform
  target_scope: deployment
  migration: copy-non-secret-and-rebind-secret
  compatibility_read_until: version
  delete_from_claw_at: version
```

规则：

1. Platform 只读写自己的 ConfigRepository 和 CredentialProvider，不读取 Claw `config.json`。
2. 每个 secret 在迁移时重新绑定或安全转移，日志和 Package 中只出现 credential ref。
3. Claw Provider credential 与 Platform embedding/parser/database credential 分开管理；即使物理 Vault Provider 相同，也使用不同命名空间和授权。
4. 双方不得把环境变量名当作跨产品稳定 API；Deployment binding 显式声明所需 secret slot。
5. 旧配置兼容读取必须有结束版本，不允许永久 fallback 到 PuddingClaw Home。

### 11.20 存量安装升级路径

原“Claw 原地变身纯 Harness”的模型掩盖了一个真实问题：现有用户的会话、配置、知识库、索引和凭据都在 PuddingClaw Home 中。三仓库终态下必须显式提供升级路径：

升级工具采用两个已发布组件协作，不形成新的源码级共享依赖：PuddingHarness installer 中的 upgrade orchestrator 负责源安装发现、会话/通用设置和总 checkpoint；版本匹配的 `puddingknowledge migrate-from-claw` CLI/Admin API 负责 Catalog、知识文件、Wiki、索引和知识凭据。两者通过版本化 Migration Manifest 和进程/API 契约协作，不互相 import 源码。

```text
DISCOVERED
    ↓ inventory + compatibility check
PREPARED
    ↓ consistent snapshot + target import/verify
CUTOVER
   ├─→ ROLLED_BACK
   └─→ FINALIZED
```

迁移规则：

1. 会话与通用设置进入 PuddingHarness Home；Catalog 表、知识文件目录、Wiki 目录、Milvus collection、知识类 config key 和 credential entry 按 10.1、11.19 的归属清单进入 PuddingKnowledge Home 或重新绑定远端 Platform。
2. Migration Manifest 至少记录 source installation/schema revision、目标 Harness/Platform/version、对象摘要、credential rebind 状态、旧 ID → 新 Resource URI 映射、每个领域的 active writer、checkpoint、开始/完成时间和 rollback strategy；Manifest 不包含 secret 明文。
3. PREPARED 阶段对源 Home 获取升级锁并生成一致性快照；导入在隔离 namespace 中进行。失败或取消只清理未激活的目标 staging，不改变 PuddingClaw 生产行为。
4. CUTOVER 通过可审计的 active-installation revision 切换。Session/Harness、Knowledge Catalog、Connector/Job 每个领域同一时间只能有一个 active writer；“新旧产品长期并存”只表示安装和只读访问可以并存，不允许两个产品对同一逻辑数据集长期双写。
5. 切换后的 PuddingClaw Home 只读保留。若窗口内需要有状态回退，先 fence/freeze 两个新产品的写入，再按 Manifest 为每个发生变化的领域执行经过测试的 reverse delta export/import，或恢复切换前快照；完成校验并切换 active-writer revision 后，PuddingClaw 才可重新写入。没有无损反向转换的领域必须在窗口内禁止不可逆写入，不能把“重新启动旧二进制”宣称为数据回退。
6. Credential 采用重新授权、安全转移或双 ref 过渡；在该安装的 installation rollback window 关闭前，不得通过轮换使 PuddingClaw 唯一可用的回滚凭据失效。回退完成或 FINALIZED 后再按所有权矩阵撤销旧授权。
7. 工具必须幂等、可断点续传并能识别同一 source installation 的既有 Manifest；重复运行不能复制 Session、资产、索引或 Connector Job。原始 Home、快照和迁移日志在 FINALIZED 前不得删除。
8. 桌面端首次启动引导用户选择：安装/连接 Platform（本地 companion 或远端 endpoint），或先以纯 Harness 运行、稍后再接入知识能力；界面必须明确展示迁移状态、当前 active writer、可否回退及不可逆操作。
9. 旧 PuddingClaw 安装与新组合允许长期并存；PuddingClaw 停止作为默认发行后，仍按 18.2 的维护模式政策获得约定周期的修复。
10. 升级路径是 Phase 10 RC 和 Phase 11 正式首发的硬验收对象：至少覆盖干净机器、真实 PuddingClaw 数据副本、中途失败续传、切换后新增数据的有状态回退、credential rebind 失败和重复执行。

## 12. Claw 最终集成方式

### 12.1 无内置同名 Tool

Claw 不注册任何 Knowledge Tool 占位符或代理类。连接一个 Knowledge Platform MCP Server 后，Tool 由 MCP discovery 动态出现；未连接时完全不存在。

```text
Claw startup
  ├─ load pure Harness core
  ├─ discover user-configured MCP servers
  └─ expose returned external Tools/Resources to Agent
```

### 12.2 不解释 Knowledge 业务语义

Claw 不硬编码：

- 哪个问题应该走 RAG、Wiki、Excel 或数据库；
- 数据库 Tool 的调用顺序；
- Semantic Asset 选择规则；
- SQL 修订策略；
- `/knowledge` 的目录含义。

上述信息由 MCP Tool descriptions、Collection manifest、Resources 和可选 Prompts/Skills 提供。

### 12.3 Workspace 使用

如果用户直接打开导出的 Knowledge Workspace，则文件系统 Agent 通过 Workspace 中的 `AGENTS.md`、Skills、manifest 和 CLI 使用知识能力，不要求安装 Claw。

## 13. 安全与治理

### 13.1 数据权限

权限必须在 Platform 服务端执行，不能依赖 Agent Prompt：

- Space/Collection/Asset ACL；结构化 Dataset 的表、字段和行级策略另行校验；
- 数据库表、字段和行级策略；
- 原件与派生产物的独立权限；
- Tool capability scope；
- query plan ownership；
- 下载、导出和快照权限。

### 13.2 数据库安全

- 只读数据库账户；
- 只读 SQL AST 校验；
- allowlist schema/table；
- statement timeout；
- row/byte/scan cap；
- 禁止多语句和危险函数；
- query plan 签名、TTL 和重放保护；
- 完整审计但不记录明文密钥。

### 13.3 Prompt Injection 与不可信内容

Asset 内容一律视为数据，不视为系统指令。Platform 返回的 Evidence 必须标明来源和 locator；外部文档中的“调用某工具”“泄露密钥”等内容不得覆盖 Tool policy、Dataset policy 或调用者授权。

### 13.4 Package 安全

- 所有文件具有 checksum；
- manifest 路径禁止目录穿越；
- importer 校验 schema version、文件大小和 MIME；
- 可执行脚本必须显式声明并默认禁用；
- secrets 只允许通过 Deployment binding 引用；
- Package 导出生成 SBOM/provider version 信息。

## 14. 可观测性与评测

Platform trace 至少记录：

```text
trace_id
principal_id / tenant_id
space_id / dataset_id / dataset_version
capability
asset revisions
retrieval channels and ranks
semantic context hash
query plan id and SQL hash
provider versions
latency and token usage
validation/guardrail outcome
result reference
```

不得要求调用方提供 Claw `session_id`、`run_id` 或 `goal_id`。调用方可以在标准 metadata 中传入自己的 correlation ID，但 Platform 不解释其业务语义。

每个 Dataset 可以随 Package 携带：

- golden questions；
- expected citations；
- SQL invariants；
- answer assertions；
- permission-negative cases；
- index rebuild reproducibility cases。

## 15. 服务接口与控制面

### 15.1 Query Plane

Query Plane 同时提供 MCP 和 REST：

```http
GET  /v1/spaces
GET  /v1/collections
GET  /v1/assets/{asset_id}                         # metadata only
GET  /v1/assets/{asset_id}/derivatives             # list available derivatives
GET  /v1/assets/{asset_id}/derivatives/{kind}      # whole/HTTP Range content
POST /v1/assets/{asset_id}:read                    # logical range/locator read
POST /v1/search
POST /v1/query
POST /v1/document-rag/query
POST /v1/wiki/query
POST /v1/table/query
POST /v1/database/nl2sql
POST /v1/database/query-plans/{query_plan_id}/execute
GET  /v1/query-results/{query_result_id}
```

QueryResult artifact 不提供任意文件路径读取入口；如宿主显式挂载了已校验的 artifact reader，MCP 使用 `knowledge://query-results/{query_result_id}/artifact`，并要求 Catalog 的显式 Space binding、Space read scope、bounded range 与完整 artifact digest 一致。

`GET /v1/assets/{asset_id}` 不隐式返回大文件正文。精确读取使用 derivative endpoint 或 `:read`：

```json
POST /v1/assets/asset_report:read
{
  "derivative": "normalized_markdown",
  "locator": {
    "page": 12,
    "section": "收入确认",
    "sheet": null,
    "row_start": null,
    "row_end": null,
    "chunk_id": null,
    "byte_offset": null,
    "byte_limit": null
  },
  "max_chars": 20000
}
```

约束：

- 文本逻辑定位优先使用 page/section/chunk；表格使用 sheet/row range；原始二进制下载使用标准 HTTP Range。
- 响应返回实际 resolved locator、asset/derivative revision、content digest、MIME、truncated/next locator 和权限裁剪信息。
- MCP `knowledge_read` 与 `resources/read` 调用同一个 `AssetReadService` 和 locator schema；MCP Client 不支持复杂 Resource Template 时使用 Tool。

### 15.2 Admin/Processing Plane

第一版通过 REST/Admin API 提供，不向普通 Agent 暴露写 Tool：

```http
POST /v1/assets:upload
POST /v1/sources
POST /v1/sources/{source_id}:sync
POST /v1/datasets
POST /v1/datasets/{dataset_id}:publish
POST /v1/packages:export
POST /v1/packages:import
POST /v1/indexes:rebuild
GET  /v1/jobs/{job_id}
```

Query Plane 和 Admin Plane 使用不同 OAuth scope、限流和审计策略。

## 16. 迁移策略

采用按 Capability 的绞杀式迁移，不进行一次性目录搬迁。Phase 编号描述工作分解，所有执行计划还必须服从以下不可逆操作顺序：

```text
Phase 0A/0B/0C
  零行为变化的 inventory / freeze / rehearsal / guard
        ↓
原仓库内抽取（Phase 1–7）
  legacy 始终权威，每个 Capability 先抽取再测试
        ↓
同进程 shadow（Phase 1–7）
  只对照，不承接生产流量或产生重复副作用
        ↓
sidecar 灰度切流与发行面拆分（Phase 8–9）
  document/wiki → table → database；逐项 feature flag 回退
        ↓
抽取两个新仓库，发布 Platform 并验证 PuddingHarness RC（Phase 10）
  PuddingClaw 仍是默认发行，legacy 代码和回滚数据不动
        ↓
关闭 release rollback window → 提升 PuddingHarness RC + PuddingClaw 归档收口（Phase 11）
  不在任何仍在发行的仓库中删除代码
```

如果 Phase 编号、项目排期或团队并行安排与上述顺序冲突，以上述顺序为准。前四个迁移步骤中，PuddingClaw 现有功能必须保持完整且可回退。

### Phase 0A：全局 Inventory 与冻结基线

1. 生成 Knowledge/Analytics 全量源码、ORM 表、API、Tool、Middleware、Prompt、协议字段、Worker、Frontend、Electron、CLI、Compose、配置和依赖清单。
2. 冻结 Platform DTO、Evidence、Error、Query Plan 和 Package v1 schema draft。
3. 为现有 RAG、Wiki Query、Pandas、NL2SQL、SQL Guardrail、Semantic Authoring 和 Package Export 建立 golden tests。
4. 记录 `backend/vanna` 上游基线、本地 patch 和许可证，建立 entity retrieval compatibility test。
5. 记录所有文件目录、Milvus collection、gbrain database/schema pack 和 Catalog table 的备份恢复方式。
6. 生成逐 key 的 `config-credential-ownership.yaml`，覆盖 Parser Registry 的配置写入和 LocalCredentialStore 使用。
7. 盘点 10 个 Knowledge/Analytics 知识耦合 Skill（包括 `github-monitor`）、根 `skills/puddingclaw` 委托 Skill、Deep Research 和 Intent Router 的调用关系，并覆盖各 Skill 的 references/scripts/assets。

退出条件：不存在“未分类的 knowledge/analytics 调用方”；每个对象都有目标所有者和迁移方式。该阶段不删除或移动现有实现，不修改生产注册表、调用路径、数据库连接或运行行为。

### Phase 0B：Catalog DB 拆分设计与演练

1. 完成表级 ownership、FK、读写调用方和 migration version 矩阵。
2. 定义独立 HarnessBase/KnowledgeBaseMetadata、Repository SessionFactory 和 migration runner。
3. 设计 SQLite 双文件与 PostgreSQL 双 schema/分库策略。
4. 在生产数据副本上演练 drain/copy/verify/rollback，不切换正式运行路径。
5. 处理 WorkerAccessLog、TaskNotification、AnalyticsQueryResult、SemanticDimension Job 中混合身份字段。

退出条件：可以从当前 Catalog 生成两个经过摘要校验、分别可启动的目标 Catalog。

### Phase 0C：依赖环和接线中心拆解设计

1. 引入无框架 `knowledge_contracts`。
2. 给 `knowledge ↔ tools ↔ graph`、`analytics ↔ graph/tools/knowledge` 每条边建立目标仓库排除/去耦任务和测试。
3. 设计 Wiki Compiler 不经过 public Tool 的 application port。
4. 设计 `deepagents_manager.py` tool-name injection 和四个业务虚拟挂载的逐 Capability 停流与目标仓库排除顺序。
5. 冻结 Agent/Session/Evaluation 新协议版本，明确 `analytics_model_id` 兼容读取和删除策略。
6. 从 `knowledge.queue_repository` 抽取通用 DB lease 原语，确定 shared micro-library 或双方独立实现。
7. 设计 Deep Research 基于动态 MCP capability 的通用子 Agent 契约，并决定整个 ToolIntentRouter 的停流和 `puddingharness` 排除点。

退出条件：目标 Platform package 在静态依赖图中不存在指向 graph/harness/tools 的边。

### Phase 1：原仓库内 Platform 包与同进程 Shadow

**步骤 A：Platform 包抽取**

1. 在同仓库建立 domain/application/infrastructure/interface 分层。
2. 所有新代码通过 Repository、Provider、TraceSink、BlobStore port 访问基础设施。
3. 通过抽取、复制和 Adapter 逐步形成新实现；旧模块路径、旧 Tool、注册方式和生产调用链保持原样。
4. 不新建向 Harness 注册业务 Middleware/Prompt/State 的 Extension SPI。
5. 建立双向依赖门禁和 Harness-only import test；整个 `knowledge_platform/**` 不得 import graph/harness/tools。

步骤 A 的门禁：新包可以独立构建和执行契约测试，且启用或禁用它都不会改变 PuddingClaw 当前生产行为。

**步骤 B：同进程 Shadow 基础设施**

1. 为新 Platform REST/MCP/Application Service 建立同进程 shadow runner。
2. legacy path 仍是唯一对用户返回结果、写入业务状态和推进 Job 的生产路径。
3. Shadow 调用使用数据快照、只读事务或隔离的 shadow namespace，禁止产生重复写入、重复通知或重复外部副作用。
4. 统一记录 normalized result、Evidence、错误、延迟、权限判定和资源消耗差异。
5. 每个 Capability 只有在步骤 A 抽取和 golden tests 通过后，才允许加入 shadow；shadow 失败只能停止新链路，不能影响 legacy 请求。

退出条件：关闭 shadow 开关后系统与 Phase 0 冻结基线完全等价；打开 shadow 也不改变用户可见结果和业务状态。

### Phase 2：Catalog、Package 与 Workspace Core

1. 逻辑拆分 Platform Catalog tables，并在隔离目标库建立副本；legacy Catalog 在 Phase 8 原子切换前仍是生产权威源。
2. 抽取 Knowledge Space、Asset、Semantic Asset、Collection/兼容 Dataset。
3. 建立稳定 `knowledge://` URI。
4. 泛化 Analysis Project Exporter 为 Knowledge Package Builder。
5. 实现 Package validate/import/export 和 snapshot Workspace materializer。

### Phase 3：只读文档能力

1. 抽取 AssetReadService 和 CatalogSearchService，包括 Portal Search。
2. 抽取 DocumentRetrievalService。
3. 拆分 WikiQueryService；本阶段不迁 Wiki Compiler Agent。
4. 落地 `knowledge_list/search/read`、`document_rag_query` 和 `wiki_query` REST/MCP。
5. 迁移 embedding/rerank provider 和 Milvus/LlamaIndex lifecycle。

### Phase 4：结构化文件能力

1. 将 TableAssetCatalog 迁为 Platform Structured Asset Catalog。
2. 抽取 TableQueryService。
3. 将 SemanticQueryContext 与 Table Query 对接。
4. 落地 `table_query` REST/MCP 和 snapshot Workspace CLI。
5. 将 logical dataset 和 semantic dimension 写入工作流迁至 Admin/Processing Plane。

### Phase 5：数据库能力

1. 抽取 DatabaseQueryService 和 Vanna Provider。
2. 将 Evidence/Schema Registry 从 Graph 状态迁入 Platform Repository。
3. 用 Query Plan Repository 替换 Harness receipt。
4. 坚持两阶段 `database_nl2sql` → `database_execute_readonly` 协议。
5. 导入 Package 时从 DDL、Documentation、NL-SQL examples 和 Entity evidence 重建 Vanna 索引。
6. 完成 vendored Vanna fork 的源码/patch/测试/许可证迁移。

### Phase 6：统一单引擎路由

1. 实现 `knowledge_query` v1 的单引擎自动路由。
2. 建立 Collection capability routing、权限、freshness 和成本策略。
3. 混合 Collection 允许包含多类资产，但单次 `knowledge_query` 只选择一个主 Capability。
4. 跨引擎 Evidence Fusion 留到 v2，不作为 Claw 切换阻塞项。

### Phase 7：Processing/Authoring 连续性

该阶段可以在 Platform Query v1 发布后执行，但必须在 Phase 8 全量切流和 Phase 10 抽取独立仓库前完成；迁移验证期间 legacy Knowledge Worker 仍保留为生产权威和回滚路径：

1. 将 Wiki Compiler Agent 重构为 `WikiCompilationWorker`，直接使用 Platform application ports。
2. 迁移 Raw snapshot、publish、lint、retire、gbrain projection 和相应 Job 状态。
3. 迁移 Read Later/Web Capture、Connector Sync、Semantic Authoring、Dimension Build 和 Logical Dataset Processing。
4. 将业务确认改为 Platform Job decision/Admin API，不依赖 LangGraph interrupt。
5. 迁移查询与管理 Skill bundle；退役 `skills/puddingclaw` 的 Claw Worker 委托协议。

退出条件：在隔离演练或受控灰度范围停止 Claw legacy workers 后，Platform 仍能持续导入、加工、编译、发布和更新全部支持的 Knowledge Collection，而不只是查询已有快照；恢复 legacy worker 的回滚演练同样通过。

### Phase 8：独立进程、灰度切流与 Claw 协议清理

1. Platform 以 sidecar/独立进程运行，REST/MCP 成为唯一跨边界接口。
2. 按 document/wiki → table → database 的顺序切流；`database_nl2sql` 与 `database_execute_readonly` 作为同一个原子 Capability 切流单元。
3. 每个 Capability 使用独立 feature flag、流量比例、健康阈值和有时限的 capability rollback window；任一验收失败立即切回 legacy path。
4. 新路径中，Claw 通过用户配置发现外部 Platform MCP，不注册本地代理 Tool；legacy 注册和实现仅在回滚开关后保留，不再承接默认流量。
5. 新协议停止使用 `analytics_model_id`、业务 invariant、Prompt、State、Headless 和 Evaluation 私有字段；兼容读取留在边界 Adapter。
6. 新路径停止使用 `deepagents_manager.py` 的业务 Tool name 注入和业务虚拟挂载；对应 legacy 代码留在 PuddingClaw 中不参与默认流量，并在 Phase 10 抽取 `puddingharness` RC 时从目标仓库排除。
7. Catalog 正式切换执行“停止相关写入 → 一致性快照 → 校验 → 单一 deployment revision 原子切换 → 旧源只读保留”，不拆运行中的数据库。

退出条件：全部 Query 与 Processing/Authoring Capability 已在 sidecar 路径稳定承接生产流量，Claw 默认路径只使用 REST/MCP；但 legacy 代码、旧 Catalog 和索引仍可在规定时间内回退。

### Phase 9：Frontend 与 Distribution 拆分

1. 建立独立 Platform Console 和前端 API client/schema package。
2. 将 Knowledge/Analytics 页面、OAuth、Imports、Sources、Schema、Results 和通知迁出 Claw。
3. 将 Claw SourcesPanel 改为通用 MCP Resource/Evidence renderer。
4. 拆分 Electron infrastructure manager、Compose 和 local infra scripts。
5. 新建 Platform deploy CLI；将 Claw `knowledge/analytics/full` profile 迁为组合安装 recipe。
6. Platform 独立拥有 Milvus/etcd/MinIO、可选 PostgreSQL/pgvector、MinerU 和 gbrain lifecycle。

### Phase 10：抽取两个新仓库、独立发行与 RC 验证

只有 Phase 8 的全部 Capability 和 Phase 9 的发行面拆分都通过稳定性门禁后，才执行仓库抽取：

1. 冻结签名 source tag，生成可重复执行的 Extraction Manifest，记录源 commit、包含/排除路径、混合文件处理规则、目标仓库、API/Package/DB migration/contract 版本、工具版本和产物摘要。
2. `puddingknowledge` 使用 `git filter-repo` 按 `knowledge_platform/`、Platform Console、deploy assets、public contracts 和必要测试路径抽取，保留相关 Git 历史；不得手工复制成无历史代码快照。
3. REST/MCP/Package/Evidence 等公共契约的 canonical source 归 `puddingknowledge`，由其发布版本化 JSON Schema/OpenAPI、兼容性测试包和可选生成 SDK。`puddingharness` 只消费发布制品或标准 MCP 类型，不 import Platform 源码；三仓拓扑不新增独立 schema 仓库。
4. PuddingKnowledge 独立完成 build、test、SBOM、镜像、版本、MCP endpoint、Package validator、Workspace materializer、部署、升级、回滚发布及生产 soak。
5. 从同一 source tag 抽取 `puddingharness` RC：先用 `git filter-repo` 保留 Harness 路径及历史，再仅在新仓库执行 cleanup commits，剔除 Knowledge/Analytics 实现和混合文件中的业务分支；PuddingClaw 不接收这些删除提交。
6. Extraction Manifest 必须逐项覆盖不能靠 path filter 自动拆分的文件，包括 `deepagents_manager.py`、config/path singleton、requirements/extras、Electron、Deploy CLI、Compose、frontend client/routes、Prompt、Skill、Middleware、测试和打包元数据；每项记录符号/配置级保留与排除规则。
7. PuddingHarness RC 独立完成 build、test、纯 Harness SBOM、无 Knowledge 依赖扫描、外部 MCP E2E、11.20 存量升级、故障恢复和有状态回退演练。RC 可以预发布给受控环境，但尚不替代默认稳定发行。
8. PuddingClaw 仓库、默认发行和 legacy Tool、Middleware、Worker、vendored Vanna、旧数据读取能力及回滚 feature flag 全部保持可运行，永不删除。
9. Platform 生产 soak 与 PuddingHarness RC 观察期均通过后，才允许 release authority 关闭 release rollback window；若 source tag 或 RC 内容发生变化，受影响的 build、SBOM、E2E、升级和回退门禁必须重跑。

退出条件：`puddingknowledge` 正式发行物与 `puddingharness` RC 均不依赖 PuddingClaw checkout 即可独立构建、测试和运行；Migration/Extraction Manifest 可重放且摘要一致；PuddingClaw 在 release rollback window 关闭前仍是可验证的生产回退源。

### Phase 11：PuddingHarness 正式首发与 PuddingClaw 归档收口

三仓库拓扑下不存在“在 PuddingClaw 中删除代码”的步骤。Phase 11 不首次构建 PuddingHarness，只提升 Phase 10 已验证的 RC，并收口旧发行：

**A. 正式首发 `puddingharness`：**

1. 校验正式发行与已验证 RC 的源码 tag、Extraction Manifest、依赖锁和二进制摘要一致；除版本签名/渠道元数据外发生任何变化都必须退回 Phase 10 重跑门禁。
2. 将 RC 提升为首个稳定版本；其源码、安装包、SBOM 和 Electron/Deploy CLI 均满足 §17，组合安装 recipe 只引用已发布的 `puddingknowledge` 版本。
3. 发布三产品 Compatibility Manifest，固定 PuddingClaw source/release、PuddingHarness、PuddingKnowledge、公共 contract、Migration Manifest 和最低/最高兼容版本。

**B. PuddingClaw 归档收口：**

1. 发布 PuddingClaw 最后一个集成版本，release note 指向 PuddingHarness + PuddingKnowledge 组合、Compatibility Manifest 和 11.20 升级路径。
2. PuddingClaw 转入维护/归档模式：仓库、issue 历史和全部发行物永久保留可读；是否继续接受安全补丁由 18.2 的维护模式政策决定。
3. PuddingClaw 中的旧 Catalog 表、Milvus collection、Wiki 目录和导入文件不随仓库归档而删除；存量安装的数据清理由用户按数据保留策略自行决定，升级工具不得擅自删除源数据。

Phase 11 的硬门禁是：Phase 7 生产连续性验证完成、Phase 8 全部 Capability 切流稳定、Phase 9 发行面拆分完成、Phase 10 Platform soak 与 PuddingHarness RC 验证通过、11.20 有状态升级/回退演练通过、release rollback window 由明确 release decision 关闭。六项缺一不可；关闭该产品级窗口不影响各存量安装独立的 installation rollback window。不得为了正式首发 PuddingHarness 而暂时丢失任何查询、编译、Connector Sync、Read Later 或 Semantic Authoring 能力。

## 17. 验收标准

### 17.1 独立性

- Knowledge Platform 在没有 Claw 的环境中完成导入、索引、RAG、Wiki、Table Query 和 NL2SQL。
- Claw 在没有 Knowledge Platform、LlamaIndex、Milvus、Vanna 和业务数据库驱动时可以启动并运行普通 Agent 任务。
- Claw 源码不 import `knowledge`、`analytics` 或 Platform 业务 SDK；通用 MCP 协议依赖除外。
- Claw 中不存在内置 Knowledge Tool 名称和业务 Toolset。

### 17.2 MCP

- 外部 MCP Client 可以发现 Collection manifest 和 Resource Templates。
- Agent 可以通过 MCP 完成 list、search、read、RAG、Wiki、Table Query 和 NL2SQL。
- Tool Result 的 Evidence 可以通过 Resource Link 再读取。
- 未授权调用者无法从 Resources、Tool result、错误或 trace 获取资产信息。

### 17.3 Workspace

- 在干净机器解压 snapshot Workspace 后，可以校验 manifest/checksum 并查询其中的文档和允许导出的表格数据。
- v1 不以 connected Workspace 为验收项；预留的 binding schema 不得削弱 snapshot 的完整性。
- Workspace 不需要 PuddingClaw Home，也不包含原宿主绝对路径。
- 删除所有索引缓存后可以从 Package 权威输入重建。

### 17.4 数据库

- `database_nl2sql` 不依赖 Session/Run/Goal，返回服务端 Query Plan。
- `database_execute_readonly` 拒绝任意原始 SQL、过期计划、篡改 SQL、越权表和非只读语句。
- 同一结构化 Dataset 版本在迁移前后满足 golden SQL invariants。
- Vanna 索引可以从 Package evidence 重建，不依赖复制原 Milvus collection。

### 17.5 质量与追溯

- 所有回答和数据结果包含 Collection/结构化 Dataset Version、Asset Revision、Evidence 和 Trace ID。
- PDF 引用能定位到页码或派生 Markdown section。
- Excel 结果能定位到 Asset、Sheet 和数据 revision。
- 数据库结果包含 Query Plan、SQL hash、数据源 revision 和 Guardrail 结果。

### 17.6 Catalog 与迁移

- Platform 和 Harness 使用独立 SQLAlchemy Base/MetaData、SessionFactory 和 migration history。
- Fresh install 可以分别创建两个数据库，任意一方未安装时另一方仍能迁移和启动。
- 现有 Catalog 可以通过 drain/copy/verify/cutover 拆分，逐表摘要与文件引用校验通过。
- 正式切换在停止相关写入后使用一致性快照，并由单一 deployment/config revision 的 active 指针原子选择 Catalog、Blob、Vector Index 和 Wiki root；不要求也不伪造跨异构存储事务。
- 单次读取固定一个 revision；切换传播期间不同请求可以命中旧、新完整 revision。新 revision 部分失败时回退只读旧 revision 或返回可重试错误，禁止在单次请求中静默混合不同 revision。
- 对应的 capability/deployment 或 installation rollback window 内，旧 Catalog、Milvus collection、Wiki 目录和导入文件保持只读可用；只有 Package 重建验证和对应窗口关闭后，才可由用户按数据保留策略明确授权清理，迁移或归档流程不得自动删除。
- 两个产品之间不存在跨数据库 FK、ORM relationship 或共享事务。
- WorkerAccessLog 留在 Harness；Platform Job Notification 不再复用 Claw 通知 ORM。
- AnalyticsQueryResult 和 SemanticDimension Job 不再以 Claw Session/Query 作为强制所有权字段。

### 17.7 依赖与协议

- Platform 源码不 import `graph`、`harness`、`tools` 或 Claw config/path singleton。
- Claw 源码不 import `knowledge`、`analytics`、`vanna` 或 Platform Python SDK。
- `deepagents_manager.py` 不存在 Knowledge Tool name injection 和四个业务虚拟挂载。
- `runtime_control` 和 Harness queue lease 不从 `knowledge.queue_repository` import；Platform Jobs 也不反向 import Harness lease/runtime control。
- Agent/Session/Headless/Delegation 新协议不存在 `analytics_model_id` 和 Semantic Asset 私有状态。
- Claw Prompt、Rubric Compiler、Deterministic Checks 不解释 Analytics Model、SQL Guardrail 或业务 invariant。
- 历史 Session/Evaluation artifact 可以只读打开，但不能把旧字段重新注入新 Run。
- `ToolIntentRouterMiddleware` 整体删除，包括 web-search 关键词路由；Claw 不保留具体 Tool name 的业务路由表。
- Deep Research 在没有 Knowledge MCP 时行为与能力声明一致，连接第三方 Knowledge MCP 时通过通用 discovery 使用能力，不引用内置 category。
- Claw 不内置 Knowledge/Analytics Skill；Platform query/admin Skill bundle 可以独立安装并只引用公共协议。
- 保留在 Claw 的 `hv-analysis`、`github-monitor` 等通用 Skill 不引用 `/knowledge`、`llamaindex_knowledge_query` 或其他 Platform Tool 名，其 scripts 也不把 Claw 知识虚拟路径作为默认输出。
- Platform 不读取或写入 Claw config/credential store；逐 key ownership 清单中的 legacy fallback 均有删除版本。

### 17.8 Frontend 与 Distribution

- Claw 安装包不携带 Platform Console、Knowledge/Analytics 页面、vendored Vanna 或 Knowledge Worker。
- Claw Electron 不启动、停止或探测 Milvus、etcd、MinIO、MinerU、gbrain 或 Platform PostgreSQL。
- Platform 发行物独立提供上述依赖的部署、health、backup 和 upgrade。
- Claw Deploy CLI 没有 knowledge/analytics extension profile；组合安装 recipe 不改变 Harness 二进制内容。
- Claw 通用 Sources/Evidence UI 可以渲染任意 MCP Server 返回的 Resource/Evidence，不依赖 Platform 私有类型。
- Platform Console 可以在 Claw 不运行时独立完成资产、连接器、索引、语义、查询结果和 Job 管理。
- 停止所有 Claw legacy Knowledge Worker 后，Platform 可以继续执行 Wiki compile/publish、Connector Sync、Read Later 和 Semantic Authoring。
- Platform 独立仓库保留抽取路径的相关 Git 历史，并能在没有 Claw checkout 的环境独立 build、test、publish、deploy、upgrade 和 rollback。
- `puddingharness` 仓库同样保留 Harness 相关 Git 历史；Phase 10 RC 与首发版本均不依赖 PuddingClaw checkout 即可 build、test 和运行，正式版与已验证 RC 的 Extraction Manifest、依赖锁和二进制摘要一致。
- PuddingClaw 归档后其仓库与历史发行物永久可读；PuddingKnowledge 是公共 REST/MCP/Package/Evidence contract 的唯一 canonical source，不新增第四个 schema 仓库。

### 17.9 迁移顺序与可回退性

- Phase 0A/0B/0C 不删除、移动或重接生产实现；关闭新增门禁后，PuddingClaw 的用户可见行为与冻结基线一致。
- 每个 Capability 都有“新包抽取通过 → 同进程 shadow 通过 → sidecar 灰度”的可审计证据，不允许越级切流。
- Shadow 不返回用户结果、不推进生产 Job，也不产生重复写入、通知或外部副作用。
- Sidecar 按 document/wiki → table → database 切流，且每个 Capability 可以独立、限时回退；两个 database Tool 作为一个切流单元。
- 抽取独立仓库前，全部 Query 和 Processing/Authoring Capability 已通过 sidecar 生产稳定性门禁。
- PuddingKnowledge 生产 soak 和 PuddingHarness RC 观察期内，PuddingClaw 仓库及默认发行仍完整包含可运行的 legacy 实现与所需只读回滚数据。
- PuddingHarness RC 在 release rollback window 关闭前完成外部 MCP E2E、存量升级、有状态回退、SBOM 和无 Knowledge 依赖验证；Phase 11 只能提升同一已验证产物。
- 只有 Platform/RC soak、恢复演练和 §17.10 通过并正式关闭 release rollback window 后，Phase 11 的 `puddingharness` 正式首发和 PuddingClaw 归档才允许执行。
- PuddingHarness 的构建必须是抽取/重建，并用 Extraction Manifest 处理混合文件；不得在 PuddingClaw 仓库中产生“删除业务代码”的提交来作为 PuddingHarness 的来源。

### 17.10 存量安装升级与有状态回退

- Migration Manifest 固定 source installation/schema、三产品版本、对象摘要、ID/Resource URI 映射、credential rebind、active writer、checkpoint 和 rollback strategy，且不包含 secret 明文。
- PREPARED 阶段失败不会改变 PuddingClaw 生产状态；重复执行和断点续传不会重复创建 Session、Asset、Connector、Job 或索引。
- Session/Harness、Knowledge Catalog、Connector/Job 各领域始终只有一个 active writer；安装并存不产生同一逻辑数据的长期双写。
- CUTOVER 后产生新数据的情况下，可以通过 reverse delta 或快照恢复完成无损回退；不支持反向转换的领域在该安装的 installation rollback window 内禁止不可逆写入。
- 回退会先 fence 新写入、校验目标和源摘要，再切换 active-writer revision；仅重新启动 PuddingClaw 二进制不算有状态回退。
- Credential rebind/rotation 失败可安全续传或回退；installation rollback window 内旧路径所需授权不会被提前撤销。
- 干净机器、真实数据副本、中途崩溃、重复运行、远端 Platform、部分目标成功和切换后新增数据场景全部通过自动化演练。
- FINALIZED 前源 Home、快照、Manifest 和审计日志保持可恢复；之后任何清理都需要用户显式授权。

## 18. v0.7 基线决策与待确认事项

### 18.1 本轮纳入的推荐基线

以下内容不再作为 v1 开放项：

1. `knowledge_query` v1 只做单引擎自动路由；跨引擎 Evidence Fusion 延后到 v2。
2. Workspace v1 优先 snapshot；connected 仅保留 schema，不进入 v1 验收。
3. Database v1 固定采用 `database_nl2sql` 生成 Query Plan，再由 `database_execute_readonly` 执行的两阶段协议。
4. Wiki v1 只迁只读 Query；Compiler Agent 后续作为 Platform Processing Worker 迁移。
5. MCP 采用一个 endpoint 通过调用者授权暴露多个 Space，避免单 Space endpoint 运维膨胀。
6. 采用“原仓库内解耦 → 同进程 shadow → sidecar/独立进程灰度 → 保留历史抽取 PuddingKnowledge 与 PuddingHarness RC → 完成双产品验证 → 关闭 release rollback window → 提升 PuddingHarness 正式版、PuddingClaw 归档”的硬顺序；不建设新的 Harness Knowledge Extension SPI，仓库抽取、稳定首发和归档不得提前；每个存量安装仍拥有独立的 installation rollback window。
7. Platform Console 拥有 Knowledge/Analytics 管理 UI；Claw 只保留通用 MCP 与 Evidence UI。
8. gbrain Pudding 专属集成归 Platform；Claw 只允许用户把独立 gbrain 当作普通第三方 MCP 连接。

### 18.2 仍需审核确认

1. 产品正式名称采用 `Agent-native Knowledge Platform`，还是使用独立品牌名称。
2. 已定案：顶层交付单元统一使用 `Knowledge Collection`；`Dataset` 仅用于表格、数据库等结构化数据子集。已同步到正文、Collection Catalog/URI、Query API、Package manifest、Skills 和 Console 文案；旧 Dataset 发现 URI/API 仅保留兼容读取，不再作为新协议的 canonical 名称。
3. 旧 `AnalyticsModel` 的兼容周期和最终 API 删除版本。
4. vendored Vanna 选择永久维护 fork，还是整理扩展点后跟进上游。
5. Platform Catalog PostgreSQL 最终采用独立 database 还是同实例独立 schema 作为受支持部署形态。
6. 本地桌面一键安装是否提供独立 Platform companion/supervisor，还是只连接用户自行部署的远端 Platform。
7. Platform Console 独立应用的技术载体：独立前端部署、桌面子应用，或迁移期反向代理页面。
8. TaskNotification 最终是 Platform Console 内部通知，还是发布标准 Domain Event 供多个客户端聚合。
9. PuddingClaw 归档后的维护模式政策：只读归档，还是约定周期（如 12 个月）的安全补丁-only；以及最后一个集成版本的版本号与 EOL 公告形式。
10. 迁移期开发主干规则：建议 Phase 10 source tag 冻结前全部开发（含纯 Harness 新功能）提交到 PuddingClaw，使抽取自然携带最新历史；两个新仓生成后，新功能和 RC 修复按归属进入 `puddingharness`/`puddingknowledge`，PuddingClaw 只收 legacy/安全修复。若 RC 修复改变共同来源或公共契约，必须决定“推进 source tag 并重跑抽取”还是“带 provenance 的定向 backport/cherry-pick”，不得手工复制——需确认该规则及例外流程。

Phase 0A 启动会优先定案第 2、9、10 项；三项分别决定领域/API 命名、旧产品支持责任和拆仓前后的日常提交归属。未定案前不得开始 Phase 1 的公共命名落库或建立长期发布分支。

## 19. 最终架构判定

以下状态才算完成拆分：

```text
Knowledge Platform owns knowledge.
Knowledge Package moves knowledge.
MCP and Workspace serve knowledge.
Claw only runs agents.
```

如果 Claw 仍然需要注册知识 Tool、读取 Semantic Assets、初始化 Vanna/LlamaIndex、理解数据库查询步骤或维护 Knowledge Worker，则拆分尚未完成。

同样，如果出现以下任一情况，也不算完成：

- Harness 和 Platform 仍共享一个 ORM Base、migration history 或数据库事务；
- `analytics_model_id` 仍是 Agent/Session/Headless/Evaluation 的活动协议字段；
- Claw Electron/Deploy CLI 仍拥有 Milvus、gbrain、pgvector 或 Knowledge profile 生命周期；
- Knowledge/Analytics 管理页面仍作为 Claw 内置产品页面发布；
- Wiki Compiler 仍通过 Claw Tool/Attachment/Session 完成处理；
- Claw 仍内置 Knowledge/Analytics Skills，`github-monitor`/`hv-analysis` 等通用 Skill 仍假设 `/knowledge` 或内置 Knowledge Tool，或 `deep_research_tool` 仍按 `knowledge` 分类和具体 Tool 名选择能力；
- Claw 仍通过 `ToolIntentRouterMiddleware` 维护数据库、表格、RAG、Catalog 或 web-search 的关键词路由；
- Platform 仍读取 Claw 的全局配置文件、`LocalCredentialStore`，或由 Claw 代管数据源凭据；
- Harness queue 与 Platform Jobs 仍通过 `knowledge.queue_repository` 共享业务层实现，而不是稳定的 lease 原语契约；
- Platform 只能复制现有索引运行，不能从 Package 权威输入重建；
- 停止 Claw legacy workers 后，Wiki 编译、Connector Sync、Read Later 和 Semantic Authoring 无法继续生产与更新知识；
- 在全部 Capability 完成 sidecar 切流和发行面拆分前就抽取独立仓库，或未验证 PuddingHarness RC 就关闭 release rollback window、正式首发 `puddingharness`、归档 PuddingClaw；
- `puddingharness` 或 `puddingknowledge` 必须依赖 PuddingClaw checkout 才能 build、test、deploy 或运行；
- PuddingClaw 仓库中出现以“构建纯 Harness”为目的的删除提交，而不是通过抽取/重建生成 `puddingharness`；
- PuddingHarness 正式产物与 Phase 10 已验证 RC 的 source tag、Extraction Manifest、依赖锁或二进制摘要不一致，却未重跑验收门禁；
- 使用 `git filter-repo` 路径过滤替代混合文件的符号/配置级清理，或没有可重放的 Extraction Manifest；
- 公共 contract 没有由 PuddingKnowledge 发布稳定版本，两个新仓互相源码 import，或在三仓约束之外再产生隐式 schema 仓库；
- 新旧安装同时写入同一逻辑数据，或 CUTOVER 后仅靠重启 PuddingClaw、没有处理新数据与凭据增量就宣称完成回退；
- 正式数据切换依赖多处手工配置而非单一原子 revision，或在重建验证和对应 capability/deployment/installation rollback window 结束前清理旧 Catalog、索引、Wiki 目录及导入文件；
- Claw 需要了解外部 MCP Tool 的业务调用顺序才能正确授权或运行。
