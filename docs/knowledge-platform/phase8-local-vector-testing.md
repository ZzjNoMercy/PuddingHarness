# Phase 8 本地向量候选测试

## 1. 先审计旧 Milvus 是否可复用

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_dense_reuse_audit.py
```

该命令只读本机 Milvus 的 `id`、`doc_id` 和文本摘要，输出计数/维度，不输出正文或向量，
也不会创建、修改或删除 collection。只有当前 chunk 身份、文本摘要和完整行数全部一致，
审计才会报告 compatible；`reuse_allowed` 仍固定为 `false`，避免把旧索引误当成新候选。

## 2. 构建新的 dense candidate

如果已有本机 OpenAI-compatible embedding 服务，可显式使用 loopback endpoint：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_dense_candidate.py \
  --local-endpoint \
  --endpoint http://127.0.0.1:9000/v1/embeddings \
  --model <local-model-id> --dimension <dimension>
```

`--local-endpoint` 只接受 `127.0.0.1`、`localhost` 或 `::1`，不读取 Vault，也不能与
`--allow-network` 同时使用。

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_dense_candidate.py
```

默认结果 `PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED_EXTERNAL_EMBEDDING_NOT_AUTHORIZED` 是预期的
fail-closed 状态。要继续构建，必须提供本地 embedding endpoint，或由用户明确授权
上述 `--local-endpoint` 或明确授权 `--allow-network` 并同时确认本地文档将发送到指定的
embedding provider；未获得授权时不得
读取 Vault、联网、创建 dense collection 或修改旧 collection。

## 3. 查询 dense candidate（只读 shadow）

candidate 构建完成后，可以用本地模型对一个 bounded query 做一次真实查询：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_dense_query_shadow.py \
  --catalog artifacts/phase0b-local-catalog/knowledge-platform.sqlite3 \
  --manifest artifacts/phase0b-local-catalog/phase8-local-vector-rebuild-manifest.json \
  --chunks artifacts/phase0b-local-catalog/phase8-local-vector-chunks.json \
  --model-dir /Users/pet/models/jina-embeddings-v4-vllm-retrieval \
  --dimension 2048 --max-length 1024 --limit 3
```

该命令只查询 loopback Milvus candidate，并以 `MilvusCatalogRetrievalProvider` 做 Catalog
Asset/Space fence；报告只保存结果数量、摘要 digest 和 `knowledge://` URI 计数，不保存
query、quote、物理路径或向量。它固定返回 `PASS_NOT_ACTIVATABLE`，不会写 Catalog binding、
切换 active revision 或启用生产流量。

## 4. 通过 Platform REST/MCP 做 dense shadow

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_dense_platform_http_shadow.py \
  --catalog artifacts/phase0b-local-catalog/knowledge-platform.sqlite3 \
  --manifest artifacts/phase0b-local-catalog/phase8-local-vector-rebuild-manifest.json \
  --chunks artifacts/phase0b-local-catalog/phase8-local-vector-chunks.json \
  --model-dir /Users/pet/models/jina-embeddings-v4-vllm-retrieval \
  --dimension 2048 --max-length 1024 --limit 3
```

该命令把同一个本地 dense provider 接到 Platform REST、`knowledge_query` 单引擎路由和
MCP `tools/call`，并验证无权限请求被拒绝。它只访问 loopback Milvus 和本地模型；报告只保存
bounded status/count/digest，不保存 query、quote、物理路径或 vector，固定为
`PASS_NOT_ACTIVATABLE`，不会创建 candidate、写 Catalog binding 或改变 active revision。

## 5. 通过独立 Platform 进程做 dense shadow

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_dense_platform_process_shadow.py \
  --catalog artifacts/phase0b-local-catalog/knowledge-platform.sqlite3 \
  --manifest artifacts/phase0b-local-catalog/phase8-local-vector-rebuild-manifest.json \
  --model-dir /Users/pet/models/jina-embeddings-v4-vllm-retrieval \
  --dimension 2048 --max-length 1024 --limit 3
```

该命令启动真正的 loopback Platform 子进程；子进程只在临时 Catalog 副本中绑定 dense
provider，父进程通过 REST 和 MCP 调用后验证服务端 binding、deployment revision、子进程
退出和 canonical Catalog digest。结果固定为 `PASS_NOT_ACTIVATABLE`，不会激活生产流量。

独立进程模式还会拒绝 symlink manifest、带凭据或非 loopback 的 Milvus URI；模型目录由本地
Transformers provider 以 `local_files_only=true` 校验。若任一边界不满足，子进程不会进入
ready 状态。

## 6. 汇总 Phase 8 本地能力矩阵

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase8_local_capability_matrix_shadow.py \
  --artifact-dir artifacts/phase0b-local-catalog \
  --output artifacts/phase0b-local-catalog/phase8-local-capability-matrix-shadow-report.json
```

该汇总器只读各能力的 bounded shadow 报告，检查独立进程、clean shutdown、canonical Catalog
unchanged 和 dense 的 no-activation/no-network 约束；缺报告、symlink 输入或不安全 dense
边界都会阻断矩阵，不会把本地 shadow 误标为生产切流。
