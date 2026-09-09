# Phase 9 本地 Asset 绑定测试

## 可选 Database/Vanna 统一运行时

一键入口现支持 `--database-config /absolute/path/to/database.json`。配置格式与独立安装步骤见
[`packages/knowledge-platform-runtime/README.md`](../../packages/knowledge-platform-runtime/README.md#postgresql-and-local-vanna-examples)。
未提供配置时不启用数据库；提供后资料库/Wiki 与数据库 schema、两阶段查询运行在同一 API，产品 Web 无需更换入口。
本地 Vanna 仅匹配已存在 SQL examples，尚不代表接入真实 LLM。配置示例中的身份/摘要占位符必须替换为已验证值。


本流程只使用当前本地 staged Catalog 和本机文件。它验证 Host-owned binding 如何进入 REST/MCP 和独立 Platform 进程；不会修改源 Catalog、staged Catalog、原始文件，也不会激活 Platform。

## 0. 先打开独立产品 Web 看 Collection / Asset

这是只读的本地 UI smoke；它使用当前 staged Catalog，不会修改源 Catalog 或本地原始文件。

推荐在仓库根目录一键启动：

```bash
./scripts/start-knowledge-local.sh
```

预检但不启动进程：

```bash
./scripts/start-knowledge-local.sh --check
```

启动成功后打开脚本输出的 Web URL（默认 `http://127.0.0.1:8090/knowledge`）。页面会自动
发现 Space、Collection 与 Assets，不需要填写 API 地址。脚本不会清理
被其他程序占用的端口；默认端口被占用时会自动选择下一个空闲 loopback 端口，并在输出的
运行时 BFF 中传递实际 API 地址。显式传入的端口若冲突则 fail-closed。`Ctrl+C` 只停止
脚本自己启动的两个进程。

如需指定其他本地 Wiki 或端口：

```bash
./scripts/start-knowledge-local.sh \
  --wiki-root /absolute/path/to/wiki \
  --api-port 8890 --console-port 8091
```

使用非默认端口时直接打开脚本输出的完整 Web URL。

下面保留旧静态契约诊断 Console 的分终端方式，仅用于调试 REST/MCP 边界，不作为产品 UI 验收。

终端 A：

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw
npm --prefix packages/knowledge-platform-console run build
backend/.venv/bin/python -m http.server 4173 --bind 127.0.0.1 \
  --directory packages/knowledge-platform-console/dist
```

终端 B：

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw
TMP_DIR="$(mktemp -d /private/tmp/puddingclaw-console-process-XXXXXX)"
PYTHONPATH=backend backend/.venv/bin/python \
  -m knowledge_platform.local \
  --catalog artifacts/phase0b-local-catalog/knowledge-platform.sqlite3 \
  --wiki-root /Users/pet/Documents/knowledge/llm-wiki/wiki \
  --temp-dir "$TMP_DIR/server-data" --port 8889 \
  --ready-file "$TMP_DIR/ready.json" \
  --console-origin http://127.0.0.1:4173 \
  --asset-binding-review-queue artifacts/phase0b-local-catalog/local-asset-binding-approval-queue.json
```

浏览器打开 `http://127.0.0.1:4173/`，把“Platform API 地址”填为
`http://127.0.0.1:8889`，点击“发现 Spaces / Datasets / Assets”。预期看到本地
Collection、Dataset 和 Assets；当前会看到 `45` 条 staged Catalog 记录加上 `73` 个
隔离物化的 Wiki 页面，即约 `118` 个 UI Assets，这是 shadow 副本的预期结果，不会
写回源 Catalog。选择一个 Wiki Asset 的“读取片段”可以看到 bounded 片段；挂载默认
queue 后会看到旧 restore review 的候选项。若使用下面第 1 节的 digest queue，则会看到
当前本地扫描发现的全部候选项。该区域只显示 review ID、摘要、候选数量和 Catalog Asset，
不显示本地路径，也没有批准按钮。
`--console-origin` 只接受显式 HTTP loopback origin，仅用于这个本地 shadow。

Platform 也提供只读的 `GET /v1/asset-binding-reviews?space_id=...`。只有显式传入
`--asset-binding-review-queue` 时才挂载，要求 `knowledge.admin` scope，并在每次读取
时严格校验 queue 格式、Space、Catalog 不变和 `execution_allowed=false`；queue 文件
损坏、Space 不符或出现 path 字段时统一返回能力不可用。

## 1. 生成审批前队列

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_asset_binding_review_queue.py
```

上面的默认输入是旧 restore review。要把刚才按本地 Knowledge 根目录发现的全部候选接入只读队列，使用：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_asset_binding_review_queue.py \
  --review-manifest artifacts/phase0b-local-catalog/local-asset-content-review.json \
  --output artifacts/phase0b-local-catalog/local-asset-binding-approval-queue.json
```

该队列当前为 `45` 条有候选项，其中 `7` 条标记为需要人工选择副本；公共 REST/Console 只显示候选数量，不显示候选路径。

查看：

```text
artifacts/phase0b-local-catalog/local-asset-binding-approval-queue.json
```

队列只包含 `review_id`、摘要、字节数和脱敏后的 Catalog 元数据。`approval_required=true` 且 `execution_allowed=false` 是预期状态。

## 2. 按 Asset content digest 重新发现本地文件

如果旧的 restore review 把 derived/profile 路径列为无候选，可用下面的只读扫描直接按
Catalog Asset 的完整 SHA-256 在本地 Knowledge 根目录找文件：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_asset_digest_review.py \
  --root /Users/pet/Documents/knowledge
```

输出写入 `artifacts/phase0b-local-catalog/local-asset-content-review.json`。这是 Host-only
review manifest，包含本地候选路径，只能用于下一步本地批准和 binding 准备，不得上传到
REST/MCP。当前实测为 `45` 个 Asset、`38` 个唯一候选、`7` 个多副本候选、`0` 个内容
未找到；多副本仍需人工选择，精确摘要也不等于自动批准。

## 3. 明确批准后准备 Host manifest

只把人工确认过的 review ID 传给 `--approve-review-id`；若使用上一步新生成的 digest
review，需要同时指定 `--review-manifest`：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_asset_binding_prepare.py \
  --review-manifest artifacts/phase0b-local-catalog/local-asset-content-review.json \
  --approve-review-id sha256:<confirmed-review-id> \
  --approved-by pet-local
```

如果 review item 有多个同摘要候选，必须在同一次明确批准中选择一个候选的零基索引；唯一候选不需要这个参数：

```bash
  --select-review-candidate sha256:<ambiguous-review-id>=1
```

候选索引只在 Host-only review manifest 中有意义，脚本会重新校验所选文件的路径边界、字节数和完整 SHA-256；没有选择、索引越界或把选择用于唯一候选都会拒绝。摘要相同不代表语义上就是同一份文件，仍需人工确认。

输出默认写入：

```text
artifacts/phase0b-local-catalog/local-asset-bindings.local.json
```

该文件是 Host-only、`activation=not-activated`、`execution_allowed=false` 的证据 manifest。准备失败时应保持 fail-closed，不自动修改 Catalog 或文件。

## 4. 验证本地 Package 导出边界

只有当 manifest 已覆盖目标 Collection 的全部 Asset 时，Package 才允许导出；只批准
部分 Asset 会继续返回 `PHASE2_PACKAGE_EXPORT_REJECTED_INCOMPLETE`，不会生成半包。

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase2_package_shadow.py \
  --output-dir artifacts/phase0b-local-catalog \
  --bindings artifacts/phase0b-local-catalog/local-asset-bindings.local.json
```

成功时预期状态是 `PHASE2_PACKAGE_EXPORT_PASS_NOT_ACTIVATABLE`。当前真实队列仍有
51 个无候选、12 个待人工批准候选和 1 个歧义项，所以在没有完整 binding 前，真实
Catalog 继续拒绝导出是正确结果；如果 Catalog metadata 含宿主路径或秘密标记，则会
返回 `PHASE2_PACKAGE_EXPORT_REJECTED_INVALID`，也不会放宽校验。

如果只是当前 legacy Space 描述里的已知 `/knowledge/` 虚拟挂载标记，可以显式开启
窄规则投影；未知绝对路径仍会拒绝：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase2_package_shadow.py \
  --output-dir artifacts/phase0b-local-catalog \
  --normalize-legacy-metadata \
  --bindings artifacts/phase0b-local-catalog/local-asset-bindings.local.json
```

该投影只作用于 Package 临时 snapshot，不改写 Catalog；报告会记录规则和计数，不记录
原文或宿主路径。

## 5. 验证公共 REST/MCP 边界

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_asset_manifest_http_shadow.py
```

预期状态：`PHASE9_LOCAL_ASSET_BINDING_HTTP_SHADOW_PASS_NOT_ACTIVATABLE`。

## 6. 验证独立 Platform 进程

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_asset_manifest_process_shadow.py \
  --wiki-root /Users/pet/.puddingclaw/knowledge
```

`--wiki-root` 必须替换为当前本机实际的 Wiki root；该参数只作为独立进程的本地运行输入，不会写入公共响应或报告。

预期状态：`PHASE9_LOCAL_ASSET_BINDING_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE`。报告应证明 manifest 已加载、REST/MCP digest 和稳定 URI 一致、子进程干净退出、canonical Catalog 未变化。

## 7. 不应发生的事情

- 没有明确 `--approve-review-id` 时，不得生成 binding manifest。
- Shadow 不得把 Host path、文件正文、secret 或 manifest 内容返回给 REST/MCP。
- 不得复制、移动或删除原始本地文件。
- 不得写入或切换 Catalog active revision。
- `PASS_NOT_ACTIVATABLE` 不等于生产 activation；生产前置 readiness gate 仍需单独满足。
