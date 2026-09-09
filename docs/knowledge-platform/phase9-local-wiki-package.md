# Phase 9 本地 Wiki Package/Workspace Shadow

这个 shadow 只把当前本地发布 Wiki 的 Markdown 页面编译为一个明确标注的
`Local Wiki Collection`，不声称代表完整 Catalog，也不读取或改写 Catalog、Vault、
Milvus、Vanna 或生产数据库。

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_wiki_package_shadow.py \
  --wiki-root /Users/pet/Documents/knowledge/llm-wiki/wiki \
  --package-dir artifacts/phase9-local-wiki-package \
  --package-zip artifacts/phase9-local-wiki-package.zip \
  --workspace-dir artifacts/phase9-local-wiki-workspace
```

成功状态为 `PHASE9_LOCAL_WIKI_PACKAGE_SHADOW_PASS_NOT_ACTIVATABLE`。当前实测编译了
73 个页面；Package revision 为
`sha256:e5eef6ed4ecbd063f2669259d03e0739c0d8516abc2fbb74c7132074d75abe60`。
同时可选生成 ZIP 并在临时目录导入复核；导入后的 Package revision 和 Asset 数必须
与原目录一致。

当前实际 ZIP 产物：
`artifacts/phase9-local-wiki-package-zip.zip`，SHA-256 为
`5991bc7b10b3673fa171eea715a0987813d4f634584b59ed3dc0b38542af2b54`。它导入后仍为
73 个 Asset、同一 Package revision；对应 Workspace `validate` 返回 `valid`。

生成后可以在本地验证和查询：

```bash
artifacts/phase9-local-wiki-workspace/bin/knowledge validate
artifacts/phase9-local-wiki-workspace/bin/knowledge query \
  --dataset collection_local_wiki --question "agent"
```

`validate` 只证明 snapshot 文件完整、revision 一致；`PASS_NOT_ACTIVATABLE` 仍表示
没有注册在线 Collection、没有生产切流，也没有把本地 Wiki 变成默认 Platform runtime。
当前完整本地 Catalog 的 Package 仍受 64 个物理引用阻断，因此这个 Wiki 子集不能替代
完整 Catalog export。
