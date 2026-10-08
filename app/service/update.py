"""自动更新：启动时检查远端 tag，发现新版本时供前端提示并一键更新

更新源使用「系统 git + origin」（复用既有凭据）：本项目是私有仓库，
匿名 GitHub API / raw / codeload 均取不到内容，而本地 git 凭据可正常
访问 origin；镜像 git URL 作为 origin 不可用时的回退。更新内容通过
`git fetch` + `git archive` 导出，不依赖匿名下载。
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from ..config import BASE_DIR
from ..version import APP_VERSION

# 远端操作超时（秒）
LS_REMOTE_TIMEOUT = 20
FETCH_TIMEOUT = 120
ARCHIVE_TIMEOUT = 120
# 进程内检查结果缓存时长（秒）
CACHE_TTL = 600

# 镜像前缀：origin 为 GitHub 地址时推导出对应的镜像 git URL
_MIRROR_PREFIXES = (
    ("ghproxy.net", "https://ghproxy.net/"),
    ("gh-proxy.com", "https://gh-proxy.com/"),
)
# 匹配 https://github.com/<owner>/<repo> 或 git@github.com:<owner>/<repo>
_REPO_RE = re.compile(r"(?:https?://|git@)github\.com[/:]([^/]+)/([^/.]+?)(?:\.git)?/?$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# 进程内缓存与后台检查状态（模块级，防多进程重复启动由各进程各自承担）
_cache = None
_cache_at = None
_bg_started = False
_bg_lock = threading.Lock()


def _git(args, timeout):
    """统一执行 git 命令：项目根目录为 cwd，禁用凭据交互避免挂起等待输入"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args], cwd=BASE_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env)


def _sanitize(msg, url):
    """错误信息中剔除远端 URL，避免意外泄露 URL 内嵌的凭据"""
    if url:
        msg = msg.replace(url, "<remote>")
    return msg.strip()


def _repo_url():
    """读取 origin 的 git URL；失败返回 None"""
    try:
        r = _git(["remote", "get-url", "origin"], 10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _iter_sources():
    """按优先级产出 (source, url)：origin 第一，镜像回退"""
    origin = _repo_url()
    if origin:
        yield "origin", origin
    if origin:
        m = _REPO_RE.match(origin)
        if m:
            base = f"https://github.com/{m.group(1)}/{m.group(2)}.git"
            for name, prefix in _MIRROR_PREFIXES:
                yield name, prefix + base


def _ls_remote_tags(url):
    """git ls-remote --tags：解析 refs/tags/*，忽略带 ^{} 的重复行；失败返回 None"""
    try:
        r = _git(["ls-remote", "--tags", url], LS_REMOTE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    tags = []
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        ref = line.split("\t", 1)[1]
        if ref.endswith("^{}"):
            continue
        if ref.startswith("refs/tags/"):
            tags.append(ref[len("refs/tags/"):])
    return tags


def list_remote_tags():
    """按 origin → 镜像顺序尝试，返回 (tags, source)；全部失败返回 (None, None)"""
    for source, url in _iter_sources():
        tags = _ls_remote_tags(url)
        if tags is not None:
            return tags, source
    return None, None


def parse_version(tag):
    """v1.2.3 / 1.2.3 → (1, 2, 3)；非法返回 None"""
    m = _VERSION_RE.match(tag or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def is_test_version(tag):
    """预留：测试版本识别接口，当前恒为 False。

    后续如需「跳过测试版本」，在此识别 -rc / -beta / -alpha / -test 之类
    的标签并返回 True；check_update 已保留对该函数的调用，本 issue 不实现
    跳过逻辑。
    """
    return False


def _do_check():
    """执行一次真实检查（不读缓存）"""
    tags, source = list_remote_tags()
    result = {
        "checked": True,
        "current_version": APP_VERSION,
        "latest_version": None,
        "latest_tag": None,
        "has_update": False,
        "source": source,
        "error": None,
    }
    if not tags:
        result["error"] = "未获取到远端版本标签（远端无 tag 或网络不可用）"
        return result
    stable = []
    for tag in tags:
        ver = parse_version(tag)
        if ver is None:
            continue
        # 筛选最新版本的调用点保留 is_test_version；当前恒 False，不影响结果
        if is_test_version(tag):
            continue
        stable.append((tag, ver))
    if not stable:
        result["error"] = "远端没有符合 vX.Y.Z 格式的版本标签"
        return result
    latest_tag, latest_ver = max(stable, key=lambda item: item[1])
    result["latest_tag"] = latest_tag
    result["latest_version"] = ".".join(str(x) for x in latest_ver)
    current = parse_version(APP_VERSION)
    result["has_update"] = current is not None and latest_ver > current
    return result


def check_update(force=False):
    """返回更新检查结果；后台检查未完成时 checked=False。

    结果在进程内缓存 10 分钟；force=True 时绕过缓存重新检查。
    """
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and _cache_at is not None and now - _cache_at < CACHE_TTL:
        return dict(_cache)
    if not force and _cache is None:
        return {
            "checked": False,
            "current_version": APP_VERSION,
            "latest_version": None,
            "latest_tag": None,
            "has_update": False,
            "source": None,
            "error": "更新检查尚未完成",
        }
    result = _do_check()
    _cache = result
    _cache_at = time.monotonic()
    return dict(result)


def start_background_check():
    """后台线程执行一次更新检查，不阻塞程序启动；模块级标记防止重复启动"""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    threading.Thread(
        target=lambda: check_update(force=True),
        daemon=True, name="xmims-update-check",
    ).start()


def install_update(tag):
    """安装指定 tag：fetch → archive → 解压 → 覆盖复制到项目根。

    跳过 data/、.git/、.venv/、.XMTEMP/、backups/、exports/、.env，
    保证本地数据与配置不被更新包覆盖。任一步失败都不改动现有文件。
    """
    tmp = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="xmims-update-"))
        src = tmp / "src"
        src.mkdir()
        # 1) 从可用远端拉取该 tag（origin 优先，镜像回退）
        last_err = ""
        url = ""
        for _source, url in _iter_sources():
            try:
                r = _git(["fetch", "--no-tags", url, "tag", tag], FETCH_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as e:
                last_err = str(e)
                continue
            if r.returncode == 0:
                break
            last_err = (r.stderr or r.stdout or "git fetch 失败").strip()
        else:
            return {"ok": False, "message": f"无法从远端拉取 tag {tag}：{_sanitize(last_err, url)}"}
        # 2) 用 git archive 导出该 tag 的内容为 zip
        zip_path = tmp / "update.zip"
        try:
            r = _git(["archive", "--format=zip", "-o", str(zip_path), tag], ARCHIVE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "message": f"导出更新包失败：{e}"}
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "未知错误").strip()
            return {"ok": False, "message": f"导出更新包失败：{_sanitize(err, '')}"}
        # 3) 解压并校验更新包结构
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(src)
        if not (src / "launcher.py").is_file() or not (src / "app").is_dir():
            return {"ok": False, "message": "更新包结构不完整（缺少 launcher.py 或 app/），已中止且未改动任何文件"}
        # 4) 覆盖复制到项目根，跳过本地数据/配置类目录
        skipped = {"data", ".git", ".venv", ".XMTEMP", "backups", "exports", ".env"}
        for item in src.iterdir():
            if item.name in skipped:
                continue
            dst = BASE_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
        return {"ok": True, "tag": tag, "message": f"已更新到 {tag}，请关闭本页并重新启动程序后生效"}
    except Exception as e:
        return {"ok": False, "message": f"更新失败：{e}"}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
