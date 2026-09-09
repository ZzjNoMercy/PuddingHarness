# Knowledge / Harness 完整仓库拆分执行清单

目标：两个保留相关 Git 历史、能够独立开发、安装、构建、测试和运行的完整仓库。原 PuddingClaw 保留 legacy 实现与回滚能力。本清单跟踪执行，不替换 `agent-native-knowledge-platform-spec.md` 的验收条件。

## 当前事实（2026-09-09）

- 尚无完成抽取的 `puddingknowledge` / `puddingharness` 仓库。
- Platform 已有无原 checkout 依赖的本地 Catalog/Wiki/PostgreSQL+Vanna examples 安装验证；这不是全能力 Platform 发行物。
- Harness 的旧 23-file mixed scan 不能证明完整依赖闭包。新增 `packages/puddingharness-extraction/audit.py` 扫描拟进入目标的全部 Python runtime，应用目标 overlay 后检查被排除模块引用、业务协议符号和动态 import。
- `overlays/` 只保存未来目标仓库 cleanup 内容，没有覆盖 PuddingClaw 原文件。其来源与内容摘要记录在 `overlay-provenance.json`。
- 旧 Extraction Manifest 曾错误把 legacy Knowledge/Analytics/Vanna 和旧业务页面指定进入 Harness；已改为明确 `preserve-legacy -> PuddingClaw`。ToolIntentRouter 按规范整个排除。
- `runtime_control` 的数据库时钟/租约参数已与 Knowledge queue 解耦，旧 queue 兼容导出与行为保留。
- 已准备 66 个目标 overlay。完整 FastAPI lifespan、HTTP Session/Agent 普通 Run、真实 DeepAgents 图及 Headless 边界已在开发 Python 环境和离线模型下通过。默认 Harness SQLite 版本 2 初始化与错误库只读拒绝、连接/租约释放已验证。
- Harness 前端已在隔离 stage 内独立 `npm ci`（无 node_modules 复用）、TypeScript 和 Next build 通过，生成 24 个页面。582 个交付文件和 11 个当前 overlay 的摘要已核对；可重跑入口见 `packages/puddingharness-extraction/frontend-staging.md`。这仍是开发抽取产物，没有完成 Git 历史拆仓或真实浏览器跨服务验收。
- `execute_skill` 在当前源 DeepAgents 本来不对外暴露；不能把 allowlist 中存在工厂名字记作可用能力。Skill secret/runtime 工具由 DeepAgents 显式绑定，后续目标运行测试必须证明此绑定仍在。

最新静态证据：`artifacts/repository-split/harness-python-audit-round11.json`，287 selected / 809 excluded / 4 findings，仍为 `blocked`。142 项 Python 测试通过；独立 locked/non-editable 后端安装中 7 项真实图、HTTP Agent、DB/Headless/租约边界通过；519 个安装文件与当前有效目标摘要一致，证据为 `artifacts/repository-split/harness-backend-install-round1.json`。剩余 4 项为两个只读兼容 selector 字面量及两个固定 registry 动态 import。Skill 自有 import 根已用真实启动目录验证；旧产品环境变量已同步，两个 sandbox probe 字符串确认只是本地文件标记后直接改为目标完整字面量，没有拼接隐藏扫描结果。

前端沿用已核验的独立 npm ci/TypeScript/Next build 证据，24 页面、582 文件、Evaluation hook 11 场景；本轮未修改前端。Knowledge 独立安装的结构化来源白名单、Logical Dataset author/publish/query、跨 Space 拒绝和 CLI 连续进程重启通过，53 项相关测试见 `artifacts/repository-split/knowledge-structured-install-round2.json`。它仍按显式输入生成私有 Catalog snapshot，未成为完整生产生命周期。

本轮历史读取边界：新 Evaluation 使用 protocol-2.0；protocol-1.0 experiment 仅内存投影，repository 按原始 JSON 行阻止执行、删除、更新、取消和 attempt 写入，避免 model_dump 丢失内部标记后绕过。Session 已移除 SQL 业务 ledger 接口和专用交接字段，旧本地 SQL 结果仍只读分页，不重执行；通用 evidence/validation receipt/Goal/Delegation 保留。useEvalStream 改为先创建 Session 再调用 Agent，仅非空 completed 最终答复且五维评分一致、保存成功后完成；人工输入、EOF、错误和预算截断均不能保存成功结果。

对抗审查关闭：SQLite WAL 在错误库校验前写入、literal %2F 路径校验错库、版本历史与实际表结构不一致、Evaluation 环境变量半迁移及历史标记丢失、staging 遗漏 overlay 摘要和安装状态、Session 未进入 overlay 的旧 schema registry 调用。完整有效树复核后排除了两个数据库证据 registry、旧 mixed catalog migration 与 8 个 Platform query/admin Skill；hv-analysis/github-monitor 通用骨架保留。

下一组同步点：独立 Worker CLI/桌面/部署合同；Research/MCP 通用宿主能力继承及 Resource 互通；完整 Platform Processing/Authoring/Query 发行验证；共同来源历史抽取与升级回滚。两个最终仓库仍未完成。

## 必须关闭的验收项

| 交付/约束 | 规范来源 | 必须取得的证据 | 状态 |
| --- | --- | --- | --- |
| 保留两个目标相关历史与共同来源 | §16 Phase 10.1–2、10.5 | 源 commit/tag、filter-repo 参数、commit-map、目标 log、可重放树摘要 | 未完成 |
| 源仓完整保留 | §11.8、Phase 10.8 | 原 legacy 路径和回归、无目标 cleanup 删除反向写回 | 持续验证 |
| Knowledge 全部 Query 能力 | §11.2–11.7、§17.1–17.5 | 独立安装后的 Document/Wiki/Table/Database/Package/Workspace 与拒绝用例 | 部分本地验证 |
| Processing/Authoring 连续性 | §11.9–11.10、Phase 7–8 | Import/Read Later/Connector/Compile/Semantic/Logical Dataset 实际生命周期与恢复 | 未完成完整独立验证 |
| Harness Agent/Session/Headless/Delegation | §11.11、§17.7 | 完整 runtime 启动与真实运行，不含活动 analytics_model_id/业务 receipt | 运行链/活动SQL账本与独立安装已验证 |
| Harness Tools/MCP/Research | §11.8、11.18、§17.2 | 无业务工具/路由/虚拟挂载；动态 MCP tools/resources + Research E2E | 固定工厂已验证；Research继承及Resource互通未完成 |
| Harness 通用 Evaluation 保留 | §11.16 | 原通用 trajectory/rubric/latency/token 回归通过，业务 invariant 迁出 | 未完成 |
| 独立 Catalog/Base/SessionFactory/migrations | §17.6 | 两环境 DB 初始化、迁移与反例，无跨库 FK/共享事务/源码依赖 | Harness SQLite通过；PG/完整升级未完成 |
| 两个完整前端 | §11.12、§17.8 | 无原 checkout 的两套 build；真实浏览器流程；Harness 无业务页面 | Harness独立stage构建通过；Platform完整UI及浏览器互通待验证 |
| 两套发行/部署/基础设施 | §11.13、§17.8 | 独立锁、SBOM、镜像/桌面/CLI、deploy/status/migrate/import/export/index | 未完成 |
| Provider/credentials 独立 | §11.14–11.15、11.19 | 独立 provider/secret binding，Harness 无 gbrain/Milvus 等 lifecycle | 未完成 |
| 外部 MCP 黑盒互通 | §11.16、Phase 10.7 | 两仓独立服务之间调用及权限/断线/Resource/Evidence 负例 | 未完成 |
| 真实存量升级与有状态回退 | §11.20、§17.10 | 干净安装、真实数据副本、中断续传、幂等、凭据失败、切换后新增数据回退 | 尚仅有部分 shadow |
| 生产切流/soak/正式发行 | Phase 8、10、11、§17.9 | 实际部署观测、签名发行与明确 release decision | 未满足；不能以本地测试替代 |

## 当前实施顺序

1. 修正目标归属、建立全树闭包检查，并把混合文件清理写成目标 overlay。
2. 闭合 Harness app/config/DB/MCP/DeepAgents/协议/Research/Evaluation，保留通用运行能力；不能删除整个通用域来让扫描变绿。
3. 完成两套 UI、部署/CLI/依赖锁与全部 Platform runtime capabilities；用隔离安装环境验证。
4. 根据已明确的开发拆仓顺序，用共同来源的 Git 历史抽取；生成并核对来源映射、cleanup commits 和可重放清单。生产迁移/正式发行仍按对应门禁执行。
5. 对照上表和规范逐项审计最终两个仓库。任何缺少直接证据的项都保持未完成。

## 可重跑入口

```bash
python3 packages/puddingharness-extraction/audit.py --output /private/tmp/harness-audit-new.json
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q packages/puddingharness-extraction/test
node packages/puddingharness-extraction/test/frontend-boundary.test.mjs
```

审计报告的 `python_static_clean` 即使出现，也只表示选定 Python runtime 的静态检查通过，不能提升为完整仓库完成。Python tests、Skills 运行目录、前端、发行资产和运行时 import 仍分别需要证据。

2026-09-09 增量：Coordinator 目标 overlay 已同步退役选择器接口，27 overlays / 81 tests passed；审查发现的旧 Analytics 验收证据注入已修复并有反例测试。round6 仍 blocked（248 findings）；DeepAgents、Headless、Delegation、Evaluation 与完整前端/发行闭包继续保留为未完成项。

2026-09-09 运行链增量：38 overlays / 98 Python tests passed；真实图+离线模型验证普通文件读取 Run，Headless 输入等待/恢复与独立活动表 IO 通过。完整 app import 仍被旧 Chat runtime 阻断，两个最终 Git 仓库与正式发行均未完成。

2026-09-09 完整 Harness 应用增量：59 overlays / 120 Python tests；实际 HTTP Agent + 独立 SQLite 初始化、失败清理及重复 Home 占锁拒绝通过；Evaluation2.0/历史只读和活动SQL ledger退役完成本轮边界验证。Harness前端独立npm ci/tsc/Next build通过，24页面/582文件摘要已核验。round10仍blocked，两个最终Git仓库、完整Platform、MCP互通与生产验收保持未完成。

最终只读复核补充：五维评估取消不能回滚已发送的有效 completed 结果保存请求；UI不会据此误报成功，但保存阶段取消的展示语义仍可改进。`evalApi.ts`由有效前端base树携带，`api.eval_api`已注册对应通用保存端点，不能因没有overlay误判缺文件。


2026-09-09 独立安装与结构化生命周期增量：66 overlays / 142 Harness Python tests / 7 independently installed runtime tests；完整环境迁移后保持 source 59 个基线文件摘要不变。一次 env 批量替换误覆盖了四个已清理 target：DeepAgents 通过原转换脚本重放命中基线 SHA，Headless 通用 registry、等待保护与拒绝语义恢复后复验；旧 Home 负例断言恢复并增加 owned DB/Session 持久化断言。staging 目录/符号链接/敏感资源/锁文件/静态结果不得提升完整发行资格的反例已覆盖。

Knowledge 已把真实 Table Query / Logical Authoring / durable Processing 接入独立本地 CLI。对抗式实测关闭了未配置来源 author、静态绑定忽略 Space、端口 TIME_WAIT 不能重启、Wiki snapshot 重复主键四项问题。连续两个独立进程重放同一发布 job、保留 source Catalog/CSV、拒绝他源 ID 覆盖通过；完整 Processing/Authoring、最终两个 Git 仓库及生产验收仍未完成。


2026-09-09 Research / MCP 宿主边界增量：67 overlays；最终完整 Harness Python suite 172 项通过，最新 locked/non-editable 独立安装 11 项真实图/HTTP/DB/Headless/Research/Resource 权限运行测试通过，520 个安装文件摘要一致。静态 round13 为 288 selected / 814 excluded / 4 findings，仍 blocked。证据：`artifacts/repository-split/harness-backend-install-round2.json`、`harness-python-audit-round13.json`。Research 继承父模型、workspace backend、Run 权限及 middleware，禁止递归和写工具，硬 10 次工具预算；真实 MCP 拒绝暂停和 Resource 批准后恰好执行一次已验证。Resource 显式 server 绑定、完整 URI 授权摘要、配置深复制、取消传播、ToolMessage error 状态和 SDK/ASGI Platform 互通通过。对抗审查修复了忽略 model_copy 返回造成的测试假阳性、资源失败误记 evidence、嵌套 headers 浅复制三项问题。

历史抽取已建立临时独立 source clone `/private/tmp/pudding-split-source-prep-round1`，基准 HEAD 保持 `5c03ad3f68e2789d6d5c7110995b580d00abc832`，尚未提交选定快照或执行 filter-repo。开发 source snapshot 明确纳入未跟踪 Platform 与目标 overlay，并保留相关 Git 历史；正式签名发行、生产激活及两个完整仓库仍未完成。MCP 当前 SDK fixture 验证不能替代两个独立服务的黑盒进程验收。


前端最终清单更正：原 582 个文件统计包含其他端口的 `.next-*` 缓存，不能作为纯交付源码数量。已修复 staging 为排除全部 `.next-*` 目录，新增不同端口反例；新独立 npm ci / tsc / Next build 通过，实际 258 个源码文件、11 个 overlay、0 个缓存交付文件，逐项摘要核对通过。以 `artifacts/repository-split/harness-frontend-build-round2.json` 和 `/private/tmp/puddingharness-frontend-stage-round13/harness-frontend-artifact-manifest.json` 为准。
