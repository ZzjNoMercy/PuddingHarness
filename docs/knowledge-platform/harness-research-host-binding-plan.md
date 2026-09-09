# Harness Research 宿主绑定下一阶段

2026-09-09，只读对抗审查；尚未实施，不能计为 Research/MCP 互通完成。

## 已确认的当前路径

`overlays/backend/tools/deep_research_tool.py` 每次另建 Agent role 模型，调用普通 `create_agent`，从全局 factory 重新构造 read_file、terminal、fetch_url。前两者忽略 factory 的 base_dir，读取/执行上下文不等于调用者 workspace；terminal 可以写入，不能称只读。目标 `agent_custom_tool_names()` 尚未包含 deep_research，所以它当前没有进入 DeepAgents 主调用链。

Research 捕获异常、没有最终 AI 文本或空输出时，也可能返回普通字符串，再被外层包装成 result。十次工具预算只是提示词和日志，没有硬执行。现有 factory 构造测试不是宿主能力继承或成功完成证据。

## 不变量

1. 子研究不能获得父 Run 没有的模型凭据、文件、网络或 MCP 能力。
2. 子研究不能自行重新发现全局工具或创建未绑定的 terminal/read_file。
3. MCP readOnlyHint 是服务端声明，不是授权；必须经过父 Run 的 policy/approval 执行边界。
4. 完成必须具有正常终止、非空最终正文与成功的证据读取；异常、EOF、工具失败和预算用尽不能包装成成功。

## 最小实施边界

宿主显式注入 run-bound model/provider、PermissionedCompositeBackend、workspace、session/run/query 身份与 parent execution policy。使用 DeepAgents FilesystemMiddleware 对同一个 backend 只开放必要读取工具；不要把新建 raw terminal 当成继承。若保留 execute，必须使用父 sandbox/approval 管线且如实标记执行能力。MCP 从父 Run 已启用集合选择，通过同一执行管线调用；禁止仅凭工具名字或 readOnlyHint 绕过审批。

默认排除写文件、技能安装/修改、递归研究及新的子任务派发。宿主能力与预算就绪后，才把 deep_research 加入实际 target Agent allowlist。强制工具调用预算，并保持完成/失败结果类型独立。

## 验证

- 实际图与离线模型读取父 workspace 中唯一标记；默认 unscoped workspace 放反例，确保没有回退。
- 同一 host context/permission policy；越界读取/写执行不得获得新权限。
- 本地 MCP fixture：未启用、未授权、缺 readOnlyHint、断线和 Resource URI 歧义的拒绝；授权成功读取进入证据。
- provider 异常、空正文、工具 error、零证据、超过硬预算各自保持失败。
- 把相同用例放入非 editable 独立安装，禁止 PYTHONPATH/target loader；随后进行两个独立服务之间的 Resource/Evidence 互通。
