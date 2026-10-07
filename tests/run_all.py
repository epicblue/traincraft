#!/usr/bin/env python3
"""一键运行 TRAINCRAFT 完整回归测试。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str]) -> None:
    print(f"\n=== {label} ===", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    started = time.perf_counter()
    node = shutil.which("node")
    if not node:
        raise SystemExit("未找到 Node.js；请安装 Node.js 18+ 后再运行客户端回归测试。")

    run("客户端主脚本语法", [node, "--check", "assets/traincraft.js"])
    run("客户端逻辑回归", [node, "tests/client_regression.js"])
    run("Python服务器语法", [sys.executable, "-m", "py_compile", "server.py"])
    run("项目结构与服务器回归", [sys.executable, "tests/project_server_regression.py"])

    elapsed = time.perf_counter() - started
    print(f"\n=== 完整回归测试全部通过 · {elapsed:.2f}s ===")


if __name__ == "__main__":
    main()
