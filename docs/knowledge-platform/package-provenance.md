# Package provenance and local SBOM

Phase 10 的 Package provenance slice 为本地 Package 导出补齐可复核的 provider 版本信息和 SBOM，但不把它当成生产发布授权。

## 内容

`KnowledgePackageBuilder.build(..., provider_versions=...)` 接收显式的 provider/version 映射，并在 Package 根目录生成：

- `package-manifest.json`：保存 `provider_versions` 和 `./sbom.json`。
- `knowledge-package.yaml`：保存同一份 provenance 元数据。
- `sbom.json`：确定性的 CycloneDX 1.5 文档，列出 Package 构建器和显式 provider 组件。

SBOM 文件属于 Package manifest 的文件集合，因此会参与文件 checksum 和 `package_revision`；篡改 SBOM 会被 Package validator 拒绝。没有 provider provenance 的旧 Package 仍按兼容路径读取，不强制要求新字段。

## 边界

版本值必须是可移植的非空文本；key/value 不得包含凭据、secret assignment、宿主绝对路径或 `file://` URI。该 SBOM 只描述当前本地 Package 的构建 provenance，不等同于独立仓库的 release SBOM，也不解除 Phase 0A/0B/1 的生产门禁。

## 验证

```bash
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q \
  backend/tests/test_knowledge_platform_package.py \
  backend/tests/test_knowledge_platform_package_provenance.py
```

完整主测试范围仍使用 `backend/tests`；仓库根目录直接收集还会遇到既有技能目录的同名测试模块和目录级导入问题。
