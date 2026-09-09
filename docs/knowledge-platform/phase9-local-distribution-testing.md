# Phase 9 本地发行面矩阵测试

该矩阵只验证当前 PuddingClaw 工作树中的 Platform Console 与发行边界，不执行仓库抽取、Docker 启动、生产切流或 activation。

运行：

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase9_local_distribution_matrix_shadow.py
```

成功的本地 shadow 状态为：

```text
PHASE9_LOCAL_DISTRIBUTION_MATRIX_PASS_NOT_ACTIVATABLE
```

矩阵包含六项检查：

1. Distribution boundary 的锚点、owner 和 scoped coverage；
2. 独立 Console 的八个发行面、文件摘要和 `built_not_deployed` 状态；
3. mixed-file 的符号级 ownership marker；
4. Claw 通用 Portable Evidence renderer 的安全边界；
5. Platform Compose/supervisor 资产和 shell 语法；
6. Console、contract、Deploy CLI、Skills 的两个临时树 offline test/build/pack 重放。

报告只保存计数、状态、摘要和安全布尔值，不保存宿主路径、业务正文、凭据或源码片段。即使全部检查通过，`activation_allowed` 与 `release_execution_allowed` 仍为 `false`；独立仓库、RC、生产 soak 和 release decision 仍由 Phase 10/11 门禁负责。
