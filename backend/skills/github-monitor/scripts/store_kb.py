#!/usr/bin/env python3
"""
GitHub Monitor — Store Script

从 stdin 读取 fetch_github.py 输出的 JSON，格式化为 Markdown 文档，
写入知识库目录。

Usage:
    python3 fetch_github.py --all | python3 store_kb.py
    python3 fetch_github.py --repo langchain-ai/langchain | python3 store_kb.py --kb-path /custom/path/
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def format_markdown(data):
    """将单个仓库的 JSON 数据格式化为 Markdown 字符串。"""
    repo = data.get("repo", "unknown")
    stats = data.get("stats", {})
    releases = data.get("releases", [])
    commits = data.get("commits", [])
    pulls = data.get("pull_requests", [])
    errors = data.get("errors", [])

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [f"# {repo} 更新追踪", "", f"> 拉取时间: {fetched_at}", ""]

    if errors:
        lines.append(f"> ⚠ 部分数据拉取失败: {', '.join(errors)}")
        lines.append("")

    # ── 仓库概览 ──
    if stats:
        lines.extend([
            "---", "",
            "## 仓库概览", "",
            f"- ⭐ Stars: {stats.get('stars', 0):,}",
            f"- 🍴 Forks: {stats.get('forks', 0):,}",
            f"- 🐛 Open Issues: {stats.get('open_issues', 0):,}",
            f"- 💻 主要语言: {stats.get('language', 'N/A')}",
            f"- 🔄 最后推送: {stats.get('pushed_at', 'N/A')[:10]}",
        ])
        desc = stats.get("description", "")
        if desc:
            lines.append(f"- 📝 描述: {desc}")
        lines.append("")

    # ── Releases ──
    lines.extend(["---", "", "## 最新 Release", ""])
    if releases:
        for r in releases:
            tag = r.get("tag_name", "")
            name = r.get("name", tag)
            display = f"{name} ({tag})" if name and name != tag else tag
            lines.append(f"### {display}")
            lines.append(f"- 发布者: {r.get('author', 'N/A')}")
            lines.append(f"- 发布时间: {r.get('published_at', 'N/A')[:10]}")
            body = r.get("body", "")
            if body:
                lines.append("- 更新说明:")
                lines.append("")
                truncated = body[:2000]
                if len(body) > 2000:
                    truncated += "\n\n...(内容过长已截断)"
                lines.append("```")
                lines.append(truncated)
                lines.append("```")
            if r.get("html_url"):
                lines.append(f"- [查看详情]({r['html_url']})")
            lines.append("")
    else:
        lines.append("暂无 Release 信息。\n")

    # ── Commits ──
    lines.extend(["---", "", "## 最近 Commit", ""])
    if commits:
        lines.append("| SHA | 消息 | 作者 | 日期 |")
        lines.append("|-----|------|------|------|")
        for c in commits:
            sha = c.get("sha", "")
            msg = c.get("message", "").replace("|", "\\|")
            author = c.get("author", "N/A")
            date = c.get("date", "")[:10]
            url = c.get("html_url", "")
            sha_link = f"[`{sha}`]({url})" if url else f"`{sha}`"
            lines.append(f"| {sha_link} | {msg} | {author} | {date} |")
        lines.append("")
    else:
        lines.append("暂无 Commit 信息。\n")

    # ── Pull Requests ──
    lines.extend(["---", "", "## 最近 Pull Request", ""])
    if pulls:
        lines.append("| # | 标题 | 状态 | 作者 | 更新时间 |")
        lines.append("|---|------|------|------|----------|")
        for p in pulls:
            num = p.get("number", 0)
            title = p.get("title", "").replace("|", "\\|")
            state = p.get("state", "unknown")
            state_icon = "🟢" if state == "open" else "🔴" if state == "closed" else "⚪"
            author = p.get("author", "N/A")
            date = p.get("updated_at", "")[:10]
            url = p.get("html_url", "")
            pr_link = f"[#{num}]({url})" if url else f"#{num}"
            lines.append(f"| {pr_link} | {title} | {state_icon} {state} | {author} | {date} |")
        lines.append("")
    else:
        lines.append("暂无 Pull Request 信息。\n")

    lines.append("---")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="GitHub Monitor — Store to an explicit caller-owned workspace")
    parser.add_argument("--output-dir", "--kb-path", dest="output_dir", required=True,
                        help="调用者显式提供的工作区输出目录；不会默认写入 Claw/Platform Home")
    args = parser.parse_args()

    kb_dir = Path(args.output_dir).expanduser()
    if kb_dir.is_symlink():
        print("❌ 输出目录不能是符号链接", file=sys.stderr)
        sys.exit(2)
    kb_dir.mkdir(parents=True, exist_ok=True)

    raw = sys.stdin.read()
    if not raw.strip():
        print("❌ stdin 无数据", file=sys.stderr)
        sys.exit(1)

    data_list = json.loads(raw)
    if isinstance(data_list, dict):
        data_list = [data_list]

    written = []
    for data in data_list:
        repo = str(data.get("repo", ""))
        if not REPO_SLUG_RE.fullmatch(repo):
            print("❌ repo 必须是安全的 owner/repo 标识", file=sys.stderr)
            sys.exit(2)
        md = format_markdown(data)
        repo_name = repo.replace("/", "_")
        fname = f"{repo_name}_tracker.md"
        fpath = kb_dir / fname
        if fpath.is_symlink():
            print(f"❌ 输出文件不能是符号链接: {fname}", file=sys.stderr)
            sys.exit(2)
        fpath.write_text(md)
        written.append(str(fpath))

    print(f"✅ 已写入 {len(written)} 个文件:", file=sys.stderr)
    for w in written:
        print(f"   {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
