---
name: github-monitor
description: 监控指定 GitHub 项目的 Release、Commit、PR 和基础统计，并将结果输出到调用者明确提供的工作区或交给 Knowledge Platform Asset ingestion。
---

# GitHub Monitor

## Goal

从 GitHub API 拉取指定仓库的最新动态，生成结构化 Markdown。这个 Skill 只
负责公开数据抓取和格式化；它不拥有 Knowledge Catalog、Wiki、索引或任何
PuddingClaw Home 目录。

## Workflow

1. 对用户明确指定的仓库调用 `fetch_url` 或同等公开 HTTP 能力；未指定时才
   使用 `references/repos.yaml` 中的默认列表。
2. 生成每个仓库的 Markdown 文档，文件名为安全的
   `{owner}_{repo}_tracker.md`。
3. 若用户要保留本地副本，必须让用户或宿主显式提供 Workspace 输出目录，
   并运行：

   ```bash
   python3 scripts/fetch_github.py --repo owner/repo \
     | python3 scripts/store_kb.py --output-dir /explicit/workspace
   ```

4. 若用户要进入知识库，使用宿主预登记的 Asset binding 调用 Platform
   upload/import contract；只提交逻辑身份、内容 digest 和 binding ID。不要
   把宿主路径放入 HTTP body，也不要直接写 Platform Catalog。
5. 向用户返回抓取摘要和 Platform 返回的 Asset/Resource URI（如有）；不能
   把生成文件自动宣称为已发布或可检索。

## Decision Tree

- 用户询问单个项目：只抓取该项目。
- 用户要求监控项目列表：只使用用户提供的列表。
- 用户只问 Star 或基础信息：跳过不需要的 Releases/Commits/PR 请求。
- GitHub 限流或网络失败：报告已有结果和失败项目；不要伪造 Platform
  Asset，也不要切换到未声明的旧 Knowledge Tool。

## Constraints

- 每次调用 `scripts/fetch_github.py` 必须指定 `--repo`、`--repos` 或 `--all`。
- 未认证 GitHub API 的限流按脚本的 bounded retry 策略处理，最多重试两次。
- `scripts/store_kb.py` 必须显式传入 `--output-dir`；没有默认 `/knowledge/`
  或其他隐式 PuddingClaw 路径。
- 输出目录和目标文件不能是符号链接；仓库标识必须是安全的 `owner/repo`。
- 输出 Markdown 是 Workspace 产物或 Platform staging 输入，不是事实源、发布
  Wiki、Milvus collection 或 Vanna 训练结果。

## Validation

- 每个成功抓取的仓库至少包含基础统计或明确错误摘要。
- Markdown 至少包含 `## 仓库概览` 和 `## 最新 Release` 或 `## 最近 Commit`。
- Workspace 写入只发生在调用者显式指定的目录；Platform 导入必须保留返回的
  staging/active 状态和 digest。

## Resources

- `scripts/fetch_github.py` — GitHub API 客户端，输出 JSON
- `scripts/store_kb.py` — 将 JSON 写入显式 Workspace 输出目录
- `references/repos.yaml` — 默认监控仓库列表，不包含输出路径
