"""源码模式的自动更新：启动时检查远端 tag，发现新版本时提示并一键更新

适用对象是按源码运行并带有 .git 的检出目录（start.bat / launcher.py）。
桌面安装版不经过本模块的检查与安装：它由 Electron 外壳直接读取 GitHub Release
并下载完整安装包，本模块只继续负责更新设置文件。

更新检查只查询 GitHub 源站（本机 git + origin，复用既有凭据）：源站确认的
标签与提交 SHA 是可信目标；下载更新内容时 origin 优先、镜像 git URL 回退，
但镜像只传输内容，实际下载到的提交必须等于源站确认的 SHA。更新内容通过
`git fetch` + `git archive` 导出，不依赖匿名下载。
"""
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from .. import db
from ..config import BASE_DIR, DATA_ROOT, DESKTOP_MODE, now_iso
from ..errors import error_dict
from ..storage.files import atomic_json, read_json
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
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

# 进程内缓存与后台检查状态（模块级，防多进程重复启动由各进程各自承担）
_cache = None
_cache_at = None
_bg_started = False
_bg_lock = threading.Lock()
_install_lock = threading.Lock()
_installed_tag = None
# 最近一次由 GitHub 源站确认的最新目标（标签 + 提交 SHA）；进程重启后没有记录
_confirmed_target = None
_PROCESS_STARTED = time.time()
_SKIPPED = {"data", ".git", ".venv", ".xmtemp", "backups", "exports", ".env"}

log = logging.getLogger(__name__)

# 用户设置：与同步设置同级存放，切换数据代次不影响
DEFAULT_SETTINGS = {"auto_check": True}


def _settings_path():
    return DATA_ROOT / "update" / "settings.json"


def load_settings():
    """读取更新检查设置；文件缺失或损坏时回落默认值"""
    try:
        saved = read_json(_settings_path(), {}) or {}
    except (OSError, ValueError):
        saved = {}
    auto = saved.get("auto_check")
    return {"auto_check": auto if isinstance(auto, bool) else DEFAULT_SETTINGS["auto_check"]}


def save_settings(values):
    """校验并落盘更新设置，返回完整设置"""
    current = load_settings()
    if "auto_check" in values:
        if not isinstance(values["auto_check"], bool):
            raise ValueError("auto_check 必须是布尔值")
        current["auto_check"] = values["auto_check"]
    atomic_json(_settings_path(), current)
    return current


def _update_record_path():
    """最近一次被接受的更新请求记录；独立于设置对象、活动代次与程序文件"""
    return DATA_ROOT / "update" / "last-update.json"


def _record_update_request(tag):
    """在下载与替换程序文件之前保存本次更新请求；写入失败由调用方中止更新"""
    version = parse_version(tag)
    if version is None:
        raise ValueError("更新标签必须为 vX.Y.Z 格式")
    atomic_json(_update_record_path(), {
        "from_version": APP_VERSION,
        "to_version": ".".join(str(x) for x in version),
        "confirmed_at": now_iso(),
        "status": "accepted",
    })


def _mark_update_installed():
    """程序文件安装完成：只改记录状态，保留接受时的起止版本与确认时间"""
    path = _update_record_path()
    try:
        record = read_json(path)
        if not isinstance(record, dict):
            return
        record["status"] = "installed"
        atomic_json(path, record)
    except (OSError, ValueError):
        log.warning("更新记录完成状态写入失败，保留已保存的接受记录")


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


def _check_url():
    """检查只信任 GitHub 源站：origin 为 GitHub 仓库时返回可信查询 URL。

    明文 HTTP 不能建立可信记录，统一改用 HTTPS；非 GitHub 的 origin 不参与检查。
    """
    origin = _repo_url()
    if not origin:
        return None
    if not _REPO_RE.match(origin):
        return None
    if origin.startswith("http://"):
        return "https://" + origin[len("http://"):]
    return origin


def _iter_sources():
    """按优先级产出 (source, url)：origin 第一，镜像回退（仅用于下载内容）"""
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
    """git ls-remote --tags：返回 {tag: 目标 SHA}，失败返回 None。

    同名 refs/tags/<tag> 与 refs/tags/<tag>^{} 同时出现时取剥离行，
    注解标签的目标 SHA 是剥离标签对象后的那一行；轻量标签取 ref 指向的 SHA。
    缺少或格式异常的 SHA 不进入结果。
    """
    try:
        r = _git(["ls-remote", "--tags", url], LS_REMOTE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    peeled = {}
    direct = {}
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if not ref.startswith("refs/tags/"):
            continue
        sha = sha.strip()
        if not _SHA_RE.match(sha):
            continue
        if ref.endswith("^{}"):
            peeled[ref[len("refs/tags/"):-3]] = sha
        else:
            direct[ref[len("refs/tags/"):]] = sha
    tags = dict(direct)
    tags.update(peeled)
    return tags


def list_remote_tags():
    """只查询 GitHub 源站，返回 ({tag: 目标 SHA}, source)；失败返回 (None, None)。

    镜像不参与发布版本选择，也不能补充或覆盖源站的标签与 SHA。
    """
    url = _check_url()
    if not url:
        return None, None
    tags = _ls_remote_tags(url)
    if tags is None:
        return None, None
    return tags, "origin"


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
    """执行一次真实检查（不读缓存）；只有源站结果能形成可信目标"""
    global _confirmed_target
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
    for tag, sha in tags.items():
        ver = parse_version(tag)
        if ver is None:
            continue
        # 筛选最新版本的调用点保留 is_test_version；当前恒 False，不影响结果
        if is_test_version(tag):
            continue
        stable.append((tag, ver, sha))
    if not stable:
        result["error"] = "远端没有符合 vX.Y.Z 格式的版本标签"
        return result
    latest_tag, latest_ver, latest_sha = max(stable, key=lambda item: item[1])
    result["latest_tag"] = latest_tag
    result["latest_version"] = ".".join(str(x) for x in latest_ver)
    current = parse_version(APP_VERSION)
    result["has_update"] = current is not None and latest_ver > current
    # 用完整记录替换旧目标；检查失败不会执行到这里，此前确认的目标保持可用
    _confirmed_target = {"tag": latest_tag, "commit_sha": latest_sha}
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


def get_confirmed_update_target():
    """返回最近一次由源站确认的最新目标副本；没有记录返回 None，不触发网络查询"""
    if _confirmed_target is None:
        return None
    return dict(_confirmed_target)


def start_background_check():
    """启动时的自动检查：用户关闭自动检查则不发起网络请求。

    桌面安装版没有源码检出可查，启动检查由 Electron 外壳完成，这里直接跳过，
    避免两条更新通道同时工作。设置文件仍由本模块读写。
    """
    global _bg_started
    if DESKTOP_MODE:
        return
    if not load_settings()["auto_check"]:
        return
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    threading.Thread(
        target=lambda: check_update(force=True),
        daemon=True, name="xmims-update-check",
    ).start()


def _target(relative):
    """限制安装/回滚目标在项目内，拒绝符号链接和目录联接。"""
    base = BASE_DIR.resolve()
    target = base / relative
    if not target.resolve().is_relative_to(base):
        raise ValueError("更新目标超出项目目录")
    for part in (target, *target.parents):
        if part == base:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("更新目标包含符号链接或目录联接")
    return target


def _incomplete_updates():
    return list((BASE_DIR / ".XMTEMP").glob("xmims-update-*/pending.json"))


def _restart_required():
    # 完成标记跨进程共享，不能在后端重启时删掉，否则仍运行的旧前端会提前解禁。
    # 已重启进程加载本模块晚于安装完成；保留旧标记不影响它继续运行。
    return any(p.stat().st_mtime >= _PROCESS_STARTED
               for p in (BASE_DIR / ".XMTEMP").glob("xmims-update-*/installed.json"))


def require_complete_installation():
    """启动前拒绝未完成的安装，不能让混合版本继续提供业务服务。"""
    if _incomplete_updates():
        raise RuntimeError("上次程序更新未完成；请先检查 .XMTEMP/xmims-update-*/pending.json 和 backup/ 并恢复，再启动程序")
    if _restart_required():
        raise RuntimeError("程序文件已更新，请退出当前进程并重新启动")


def maintenance_error():
    """供两个后端和独立前端暂停请求；磁盘标记也能被另一个进程看到。"""
    if _installed_tag is not None or _restart_required():
        return error_dict("UPDATE_RESTART_REQUIRED", "程序已更新，请关闭程序并重新启动后继续使用",
                          user_message="程序已经更新，需要重新启动",
                          user_hint="请关闭全部程序窗口后重新打开")
    if _install_lock.locked() or _incomplete_updates():
        return error_dict("UPDATE_MAINTENANCE", "程序正在更新或等待恢复，暂不能使用；若安装已中断，请恢复后重新启动",
                          user_message="程序正在更新，暂时不能使用",
                          user_hint="请等待更新结束；若长时间没有反应，请关闭后重新启动程序")
    return None


def _extract_update(archive, src):
    """校验路径后逐文件解压；本地数据和配置不进入安装计划。"""
    seen = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            path = PurePosixPath(info.filename)
            if (path.is_absolute() or not path.parts or
                    any(p in (".", "..") or ":" in p or p.endswith((" ", ".")) for p in path.parts) or
                    "\\" in info.filename or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("更新包包含不安全路径或符号链接")
            if path.parts[0].lower() in _SKIPPED:
                continue
            target = _target(path)
            data_root = DATA_ROOT.resolve()
            if target == data_root or target.is_relative_to(data_root):
                continue
            key = path.as_posix().casefold()
            if key in seen:
                raise ValueError("更新包包含重复路径")
            seen.add(key)
            dest = src / path
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, dest.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if info.external_attr >> 16 & 0o111:
                    dest.chmod(dest.stat().st_mode | 0o111)
    if not (src / "launcher.py").is_file() or not (src / "app").is_dir():
        raise ValueError("更新包结构不完整（缺少 launcher.py 或 app/）")


def _apply_update(src, tmp):
    """全量备份成功后才替换；异常回滚，恢复失败/进程中断时保留恢复目录。"""
    backup = tmp / "backup"
    files = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
    plan = []
    for relative in files:
        target = _target(relative)
        if target.exists() and not target.is_file():
            raise ValueError(f"更新目标不是普通文件：{relative.as_posix()}")
        existed = target.exists()
        if existed:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        plan.append({"path": relative.as_posix(), "existed": existed})
    # 先落盘恢复清单，再开始改文件；下一次安装发现清单时拒绝覆盖恢复资料。
    pending = tmp / "pending.json"
    pending.write_text(json.dumps({"files": plan}, ensure_ascii=False, indent=2), encoding="utf-8")
    changed = []
    created_dirs = []
    try:
        for entry in plan:
            relative = Path(entry["path"])
            target = _target(relative)
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            # 临时目录位于项目同一磁盘；单文件替换失败不会截断原文件。
            os.replace(src / relative, target)
            changed.append(entry)
        # 所有文件写入后刷新完成时间，再原子切换标记；旧前后端均保持暂停到重启。
        pending.touch()
        pending.replace(tmp / "installed.json")
    except Exception as exc:
        failed = []
        for entry in reversed(changed):
            try:
                relative = Path(entry["path"])
                target = _target(relative)
                if entry["existed"]:
                    os.replace(backup / relative, target)
                else:
                    target.unlink()
            except (OSError, ValueError):
                failed.append(entry["path"])
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass  # 不删除其他进程在期间创建的文件。
        if failed:
            raise RuntimeError(
                f"安装失败且部分文件未能回滚，请停止使用并从 {tmp} 恢复；"
                f"未恢复文件：{'、'.join(failed)}") from exc
        pending.unlink()
        raise RuntimeError("安装失败，已回滚本次替换，请排除文件占用或磁盘问题后重试") from exc


def _install_update(tag, expected_sha):
    tmp = None
    try:
        temp_root = _target(".XMTEMP")
        temp_root.mkdir(exist_ok=True)
        if _incomplete_updates():
            return {"ok": False,
                    "message": "上一次更新没有完成，请先恢复后再更新",
                    "hint": "检查 .XMTEMP/xmims-update-*/pending.json 与 backup/ 后恢复"}
        # 请求已被接受：记录必须先落盘，才能继续下载与替换程序文件。
        try:
            _record_update_request(tag)
        except (OSError, ValueError):
            return {"ok": False, "message": "无法保存更新记录，请确认数据目录可写后重试"}
        tmp = Path(tempfile.mkdtemp(prefix="xmims-update-", dir=temp_root))
        src = tmp / "src"
        src.mkdir()
        # 1) 从可用远端拉取该 tag（origin 优先，镜像回退）
        last_err = ""
        url = ""
        for _source, url in _iter_sources():
            try:
                r = _git(["fetch", "--no-tags", url, f"refs/tags/{tag}"], FETCH_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as e:
                last_err = str(e)
                continue
            if r.returncode == 0:
                break
            last_err = (r.stderr or r.stdout or "git fetch 失败").strip()
        else:
            return {"ok": False, "message": f"无法下载版本 {tag}，请检查网络后重试",
                    "hint": f"git fetch 失败：{_sanitize(last_err, url)}"}
        # 固定本次拉取的提交，避免 archive 意外使用旧本地同名 tag。
        revision = _git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], 10)
        if revision.returncode != 0:
            return {"ok": False, "message": "该版本的更新包不可用，未改动程序文件",
                    "hint": "更新标签不是有效提交"}
        actual_sha = revision.stdout.strip()
        # 导出前核对实际提交与源站确认的 SHA；不一致立即停止，不导出也不替换源码
        if not _SHA_RE.match(actual_sha) or actual_sha.lower() != expected_sha:
            return {"ok": False, "message": "版本校验失败，未改动程序文件",
                    "hint": "下载到的提交与源站确认的版本不一致，已停止安装；请重新检查更新后重试"}
        # 2) 用 git archive 导出该提交的内容为 zip
        zip_path = tmp / "update.zip"
        try:
            r = _git(["archive", "--format=zip", "-o", str(zip_path), actual_sha], ARCHIVE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "message": "导出更新包失败", "hint": str(e)}
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "未知错误").strip()
            return {"ok": False, "message": "导出更新包失败",
                    "hint": _sanitize(err, "")}
        # 3) 解压并校验更新包结构
        _extract_update(zip_path, src)
        # 4) 备份、替换，失败回滚；业务数据不在安装计划中。
        # 等待既有事务结束，再关闭业务写入；等待中的旧请求也会重新检查 ready。
        # 与同步恢复共用协调锁，避免后台同步在替换期间重新开放数据库。
        with db.coordinated_access():
            was_ready = db.business_ready
            db.set_ready(False)
            try:
                _apply_update(src, tmp)
            except BaseException:
                if not (tmp / "pending.json").exists():
                    db.set_ready(was_ready)
                raise
            # 成功或恢复不完整都保持暂停；新进程走原有启动同步流程重新开放业务。
        return {"ok": True, "tag": tag, "message": f"已更新到 {tag}，请关闭本页并重新启动程序后生效"}
    except Exception as e:
        return {"ok": False, "message": "更新失败，程序文件未被改动", "hint": str(e)}
    finally:
        if (tmp is not None and tmp.resolve().is_relative_to(BASE_DIR.resolve())
                and tmp.resolve().parent == (BASE_DIR / ".XMTEMP").resolve()
                and not tmp.is_symlink() and not (tmp / "pending.json").exists()
                and not (tmp / "installed.json").exists()):
            shutil.rmtree(tmp, ignore_errors=True)


def install_update(tag, expected_sha):
    """安装指定版本；只处理合法 tag 与源站确认的 SHA，并拒绝并发安装或未重启的重复安装。"""
    global _installed_tag
    if parse_version(tag) is None:
        return {"ok": False, "message": "更新标签必须为 vX.Y.Z 格式"}
    if not isinstance(expected_sha, str) or not _SHA_RE.match(expected_sha.strip()):
        return {"ok": False, "message": "缺少可信的版本校验信息，未改动程序文件",
                "hint": "请重新检查更新后再安装"}
    expected_sha = expected_sha.strip().lower()
    if not _install_lock.acquire(blocking=False):
        return {"ok": False, "message": "已有更新正在安装，请等待完成，不要重复操作"}
    try:
        if _installed_tag is not None:
            return {"ok": True, "tag": _installed_tag, "message": f"已安装 {_installed_tag}，请先重新启动程序"}
        result = _install_update(tag, expected_sha)
        if result["ok"]:
            _installed_tag = tag
            # 程序文件已安装完成；完成状态写入失败不影响本次成功返回
            _mark_update_installed()
        return result
    finally:
        _install_lock.release()
