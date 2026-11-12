"""基础检查：Python 与 JavaScript 语法、三个应用入口的导入。

普通 CI 与版本 tag 构建共用。只做静态检查，不启动服务、不请求接口、
不进入应用生命周期。从项目根运行：

    python -B .github/scripts/basic_checks.py

任一检查失败退出非 0。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_PYTHON_ROOTS = ("app", "desktop", ".github/scripts")
_BROWSER_JS_ROOTS = ("app/static/js", "app/static/onboarding")
_DESKTOP_JS_ROOT = "desktop"
_SKIP_DIRS = {"__pycache__", ".venv", "venv", ".XMTEMP", "data", "dist", "build"}
_IMPORT_MODULES = ("app.api_app", "app.web_app", "app.main")


def relative(path):
    return path.relative_to(PROJECT_ROOT).as_posix()


def python_files():
    files = []
    launcher = PROJECT_ROOT / "launcher.py"
    if launcher.is_file():
        files.append(launcher)
    for root in _PYTHON_ROOTS:
        base = PROJECT_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
                continue
            files.append(path)
    return files


def check_python_syntax(failures):
    files = python_files()
    failed = 0
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            failed += 1
            failures.append(f"Python 语法 {relative(path)}：无法读取源码（{error}）")
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as error:
            failed += 1
            failures.append(f"Python 语法 {relative(path)}:{error.lineno}：{error.msg}")
        except ValueError as error:
            failed += 1
            failures.append(f"Python 语法 {relative(path)}：{error}")
    print(f"Python 语法：检查 {len(files)} 个文件，失败 {failed} 个")


def node_executable(failures):
    node = shutil.which("node")
    if node is None:
        failures.append("JavaScript 语法：未找到 node 可执行文件")
    return node


def node_error(stderr):
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", "replace")
    else:
        text = stderr or ""
    # 保留到 SyntaxError 说明行，去掉 Node 自身的调用栈
    kept = []
    for line in text.strip().splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        kept.append(line)
        if line.lstrip().startswith(("SyntaxError:", "ReferenceError:", "TypeError:")):
            break
    return " ".join(kept) if kept else "未知错误"


def browser_js_files():
    files = []
    for root in _BROWSER_JS_ROOTS:
        base = PROJECT_ROOT / root
        if base.is_dir():
            files.extend(sorted(base.rglob("*.js")))
    return files


def desktop_js_files():
    base = PROJECT_ROOT / _DESKTOP_JS_ROOT
    return sorted(base.rglob("*.js")) if base.is_dir() else []


def check_js_syntax(node, failures):
    if node is None:
        print("JavaScript 语法：未检查（缺少 node）")
        return

    browser = browser_js_files()
    failed = 0
    for path in browser:
        try:
            source = path.read_bytes()
        except OSError as error:
            failed += 1
            failures.append(f"浏览器 JavaScript 语法 {relative(path)}：无法读取源码（{error}）")
            continue
        # 源码从标准输入送入，不执行、不解析 import map；失败时用原文件路径报告
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=source, capture_output=True,
        )
        if result.returncode:
            failed += 1
            failures.append(f"浏览器 JavaScript 语法 {relative(path)}：{node_error(result.stderr)}")
    print(f"浏览器 JavaScript 语法：检查 {len(browser)} 个文件，失败 {failed} 个")

    desktop = desktop_js_files()
    failed = 0
    for path in desktop:
        # 桌面模块是 CommonJS；--check 只解析语法，不加载 Electron
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True,
        )
        if result.returncode:
            failed += 1
            failures.append(f"桌面 JavaScript 语法 {relative(path)}：{node_error(result.stderr)}")
    print(f"桌面 JavaScript 语法：检查 {len(desktop)} 个文件，失败 {failed} 个")


def check_app_imports(failures):
    data_dir = PROJECT_ROOT / ".XMTEMP" / "actions" / "check-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["XM_DATA_ROOT"] = str(data_dir)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    failed = 0
    for module in _IMPORT_MODULES:
        # 只导入模块本身，不进入 lifespan、不连接数据库、不请求接口
        result = subprocess.run(
            [sys.executable, "-B", "-c", f"import {module}"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode:
            failed += 1
            detail = (result.stderr or result.stdout or "").strip()
            failures.append(f"应用导入 {module}：{detail}")
    print(f"应用导入：检查 {len(_IMPORT_MODULES)} 个模块，失败 {failed} 个")


def main():
    failures = []
    check_python_syntax(failures)
    node = node_executable(failures)
    check_js_syntax(node, failures)
    check_app_imports(failures)
    if failures:
        print()
        print(f"基础检查失败：共 {len(failures)} 项")
        for item in failures:
            print(f"- {item}")
        return 1
    print("基础检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
