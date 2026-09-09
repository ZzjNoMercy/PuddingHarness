# Phase 9 Snapshot Workspace Query

`WorkspaceMaterializer` 生成的 `bin/knowledge` 现在支持：

```bash
bin/knowledge validate
```

该命令在 Workspace 内复核 `package-manifest.json`、`checksums.json` 和所有实际文件，
拒绝摘要漂移、缺失/额外文件与 symlink；成功时返回 Package revision。它提供的是
完整性校验，不是签名验证或生产发布批准。

Workspace 同时生成不含凭据的 `bindings.local.yaml` 占位模板。它是用户本地的
override 文件，不进入 Package manifest/checksum，snapshot CLI 不读取它；因此可以
在本地填写未来 connected 模式所需的 binding，但当前不会因此获得连接、授权或执行
能力。

```bash
bin/knowledge query --dataset COLLECTION_ID --question "关键词" --limit 5
```

该命令只对 Package manifest 中声明了 `knowledge_query` 的 Collection 生效。
它读取包内 Asset，重新校验完整内容摘要，然后返回 bounded quote、正文 offset、
`knowledge://` Resource URI 和匹配分数。结果是本地 snapshot 查询，不访问网络、
Vault、旧 Catalog、Milvus 或 Vanna provider。

Query 输出同时携带 Collection version、Package revision、Asset revision、bounded
Evidence 和绑定快照 revision 的稳定 opaque `trace_id`；表格结果也携带 Asset
revision/Evidence。它们用于本地追溯，不是
connected runtime 的 Trace/QueryResult，也不证明生产部署。

查询出口还会扫描正文中的明显敏感赋值（如 `token=...`、`password: ...`、
`authorization=...`）。命中时直接失败，不返回部分 quote；这是对 Package 内容
完整性校验之外的运行时防泄漏保护。

不应把它与 connected Workspace 混淆：connected 模式仍需要独立的 endpoint trust、
credential binding、离线失败语义和执行授权设计；当前 v1 只承诺 snapshot。

生成的 Workspace 同时包含五个适配 Skill：
`knowledge-discovery`、`document-rag`、`wiki-query`、`table-query` 和
`database-query`。Skill 只描述如何调用公共能力；`database-query` 在 snapshot
模式下明确不打开 live database connection。

Database snapshot 另外提供一个仅证据匹配的候选出口：

```bash
bin/knowledge database nl2sql --dataset COLLECTION_ID --question "sales total"
```

它只从 `database/index.json` 中选择问题完全覆盖的既有 SQL example，返回包含
Collection version 与 opaque trace 的 `package_database_evidence`、`knowledge` 无关的本地 query plan 和证据记录；它不做
模型生成、不连接数据库，并将 `execution_allowed` 固定为 `false`。没有完全匹配的
example 时直接失败，不猜测 SQL；运行时还会复核 Package checksums，索引篡改时
直接失败。对应的执行形状会明确闭锁：

```bash
bin/knowledge database execute --query-plan PLAN_ID
```

snapshot v1 对该命令始终返回失败；只有未来经过独立授权的 connected Platform
database contract 才能执行，不能把本地 Package 当作生产数据库凭据或运行时。
