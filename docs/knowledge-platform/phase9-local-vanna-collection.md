# Phase 9 本地 Vanna Collection Shadow

这个流程验证 Package evidence 如何重建一个本地 Vanna Collection candidate。
它不复制旧 Milvus collection，不读取 Vault，不联网，也不会切换 active revision。

## 运行

输入必须是已通过 Package Validator 的目录：

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_vanna_collection_shadow.py \
  --package-root /path/to/knowledge-package \
  --output-dir /private/tmp/puddingclaw-vanna-shadow
```

成功状态为：

```text
PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_PASS_NOT_ACTIVATABLE
```

输出目录中会产生一个按 Package revision 区分的
`collections/<collection-name>/collection-manifest.json`，以及 `ddl.jsonl`、
`documentation.jsonl`、`sql_examples.jsonl`、`entities.jsonl`。这些是本地
candidate staging 数据，不是已注册的运行时 Collection；manifest 始终包含：

- `active=false`
- `activation_allowed=false`
- `provider_io_performed=false`
- `legacy_collection_read=false`

## 第一性原理边界

Vanna 的输入事实来自 `database/index.json`，而不是旧 Milvus 的行、向量或
collection 名称。只有 Package 完整校验成功后，才进入 candidate 写入；任何
校验失败、目标冲突或 symlink 输出目录都会 fail-closed。后续若接入真正的本地
Vanna/Milvus provider，仍须由宿主显式注入 provider identity 和 embedding 能力，
并单独建立激活、回滚与数据保留门禁。
