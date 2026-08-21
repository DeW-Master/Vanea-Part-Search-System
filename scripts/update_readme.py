# -*- coding: utf-8 -*-
"""
README 构建状态自动更新脚本。

功能：
  - 从 part-search-system/backend/config.py 读取应用版本（APP_VERSION）
  - 从 git 读取当前分支、最新提交哈希与提交信息、提交时间
  - 将上述信息写入仓库根目录 README.md 的
    <!-- BUILD_STATUS_START --> ... <!-- BUILD_STATUS_END --> 区块
  - 每次 `git push` 前由 pre-push 钩子自动调用

用法：
  python scripts/update_readme.py            # 更新 README 中的构建状态
  python scripts/update_readme.py --check    # 检查是否需要更新（不写文件，CI 用）

无第三方依赖，仅使用标准库；在 Windows / Linux / macOS 上均可运行。
"""

import os
import re
import subprocess
import sys

# 仓库根目录（本脚本位于 <root>/scripts/ 下）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
README_PATH = os.path.join(REPO_ROOT, "README.md")
CONFIG_PATH = os.path.join(
    REPO_ROOT, "part-search-system", "backend", "config.py"
)

START_MARKER = "<!-- BUILD_STATUS_START -->"
END_MARKER = "<!-- BUILD_STATUS_END -->"


def _run_git(*args):
    """在仓库根目录执行 git 命令，失败时返回空字符串。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def read_app_version():
    """从 config.py 解析 APP_VERSION。"""
    if not os.path.isfile(CONFIG_PATH):
        return "unknown"
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*APP_VERSION\s*=\s*["\']([^"\']+)["\']', line)
            if m:
                return m.group(1)
    return "unknown"


def get_git_info():
    """收集当前 git 状态信息。"""
    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    commit = _run_git("rev-parse", "--short", "HEAD") or "unknown"
    full_commit = _run_git("rev-parse", "HEAD") or ""
    subject = _run_git("log", "-1", "--pretty=%s") or ""
    author_date = _run_git("log", "-1", "--pretty=%aI") or ""
    remote = _run_git("config", "--get", "remote.origin.url") or ""
    status_porcelain = _run_git("status", "--porcelain")
    dirty = " (有未提交改动)" if status_porcelain else ""
    return {
        "branch": branch,
        "commit": commit,
        "full_commit": full_commit,
        "subject": subject,
        "author_date": author_date,
        "remote": remote,
        "dirty": dirty,
    }


def build_status_block(app_version, git_info):
    """生成要插入 README 的构建状态 Markdown 区块。

    注意：本区块的全部字段均来自「当前提交本身」（版本、提交哈希、
    提交时间等），不包含会随运行时刻变化的“当前时间”，从而保证：
      - 对同一个提交，多次运行生成的内容完全一致（幂等）；
      - 只有在产生新提交后内容才会变化，因此把 README 一起提交后，
        pre-push 钩子不会再制造“未提交的 README 改动”。
    """
    commit_display = git_info["commit"]
    if git_info["remote"] and "github.com" in git_info["remote"]:
        # 将常见的 git@github.com:a/b.git 与 https URL 统一为可浏览链接
        repo_url = (
            git_info["remote"]
            .replace(".git", "")
            .replace("git@github.com:", "https://github.com/")
        )
        if git_info["full_commit"]:
            commit_display = (
                f"[`{git_info['commit']}`]({repo_url}/commit/{git_info['full_commit']})"
            )

    lines = [
        START_MARKER,
        "<!-- 此区块由 scripts/update_readme.py 自动更新，请勿手动编辑。 -->",
        "",
        "## 构建状态",
        "",
        f"- **应用版本**: `{app_version}`",
        f"- **Git 分支**: `{git_info['branch']}`",
        f"- **最新提交**: {commit_display}",
        f"- **提交说明**: {git_info['subject'] or '-'}",
        f"- **提交时间**: {git_info['author_date'] or '-'}",
        f"- **工作区状态**: {'存在未提交改动' if git_info['dirty'] else '干净（已全部提交）'}",
        "",
        "> 本区块在每次 `git push` 前通过 pre-push 钩子按当前提交自动刷新。",
        END_MARKER,
    ]
    return "\n".join(lines)


def update_readme(check_only=False):
    """更新（或检查）README 中的构建状态区块。"""
    if not os.path.isfile(README_PATH):
        print(f"[update_readme] 未找到 README: {README_PATH}", file=sys.stderr)
        return 2

    with open(README_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    if START_MARKER not in content or END_MARKER not in content:
        print(
            "[update_readme] README 缺少构建状态标记块，已跳过。"
            f"请在 README.md 中加入 {START_MARKER} / {END_MARKER}。",
            file=sys.stderr,
        )
        return 1

    app_version = read_app_version()
    git_info = get_git_info()
    block = build_status_block(app_version, git_info)

    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    new_content = pattern.sub(lambda _m: block, content)

    if new_content == content:
        print("[update_readme] README 构建状态已是最新，无需更新。")
        return 0

    if check_only:
        print("[update_readme] 检测到 README 构建状态需要更新（--check 模式不写入）。")
        return 1

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("[update_readme] README 构建状态已更新。")
    return 0


if __name__ == "__main__":
    check = "--check" in sys.argv[1:]
    sys.exit(update_readme(check_only=check))
