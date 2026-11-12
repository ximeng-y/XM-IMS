"""依赖（Git / Git Credential Manager）安装位置的唯一实现。

记录文件为 DATA_ROOT/desktop/tools.json，只有本模块读写它：

    {"mode": "local"}                            默认局部安装，根为安装根 tools
    {"mode": "custom", "root": "<所选文件夹>/XM-IMS-tools"}
    {"mode": "global", "git": "<实际检测到的系统 Git 目录>"}

local 不保存绝对根：软件目录可能被整体移动，跟着安装根解析才不会失效。
custom 保存最终完整根，界面展示的就是它。global 只在用户明确选择系统安装
并安装成功后才写入，且写的是实际检测到的位置，不是「已启动安装器」。

中文路径判断也只在这里实现：检查目标完整绝对路径及其父路径。含空格或英文
都不算问题，前端使用后端给出的判断结果，不另写一套规则。
"""
import os
import re
from pathlib import Path

from .. import config
from ..storage.files import atomic_json, read_json

MODE_LOCAL = "local"
MODE_CUSTOM = "custom"
MODE_GLOBAL = "global"
MODES = (MODE_LOCAL, MODE_CUSTOM, MODE_GLOBAL)

# 自选文件夹下使用专用子目录，不把用户选中的整个文件夹当作工具根
CUSTOM_DIR_NAME = "XM-IMS-tools"

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def record_path():
    return config.DATA_ROOT / "desktop" / "tools.json"


def _record():
    try:
        saved = read_json(record_path(), {}) or {}
    except (OSError, ValueError):
        saved = {}
    return saved if isinstance(saved, dict) else {}


def mode():
    """当前安装模式；没有记录或记录损坏时按 local。"""
    value = _record().get("mode")
    return value if value in MODES else MODE_LOCAL


def has_chinese(path):
    """路径本身或其任一父路径含中文；只判断中日韩文字，不含空格与英文字母。"""
    if not path:
        return False
    return any(_CJK.search(part) for part in Path(path).expanduser().parts)


def local_root():
    return config.PROGRAM_ROOT / "tools"


def custom_root(path=None):
    """自选模式的最终依赖根；path 为原生窗口选中的父目录。"""
    base = path or _record().get("root")
    if not base:
        return None
    base = Path(base)
    tail = base.name.casefold() == CUSTOM_DIR_NAME.casefold()
    return base if tail else base / CUSTOM_DIR_NAME


def global_git():
    """用户明确选择系统安装后记录的实际 Git 目录。"""
    value = _record().get("git")
    return Path(value) if value else None


def git_root(target_mode=None, path=None):
    """局部 / 自选模式下的 Git 安装目录；global 由系统候选定位，返回 None。"""
    target_mode = target_mode or mode()
    if target_mode == MODE_LOCAL:
        return local_root() / "git"
    if target_mode == MODE_CUSTOM:
        root = custom_root(path)
        return root / "git" if root else None
    return None


def gcm_root(target_mode=None, path=None):
    """局部 / 自选模式下独立 GCM 的安装目录；global 规则同 git_root。"""
    target_mode = target_mode or mode()
    if target_mode == MODE_LOCAL:
        return local_root() / "gcm"
    if target_mode == MODE_CUSTOM:
        root = custom_root(path)
        return root / "gcm" if root else None
    return None


def resolve(target_mode=None, path=None):
    """本次安装的目标描述；不改记录，不检查组件是否可执行。"""
    target_mode = target_mode or mode()
    if target_mode not in MODES:
        raise ValueError("安装模式必须是 local、custom 或 global")
    if target_mode == MODE_GLOBAL:
        return {"mode": MODE_GLOBAL, "root": global_git(), "git_root": None,
                "gcm_root": None, "requires_path_choice": False}
    root = local_root() if target_mode == MODE_LOCAL else custom_root(path)
    if root is None:
        # 自选模式缺少父目录属于请求缺项，不能悄悄换成局部目标
        raise ValueError("选择其他文件夹时缺少目标文件夹")
    return {"mode": target_mode, "root": root, "git_root": git_root(target_mode, path),
            "gcm_root": gcm_root(target_mode, path), "requires_path_choice": has_chinese(root)}


def view():
    """界面需要的当前目标信息；不暴露记录里的其它内容。"""
    current = mode()
    try:
        target = resolve(current)
    except ValueError:
        # 记录里的自选根缺失：按局部目标展示，安装时要求重新选择
        current = MODE_LOCAL
        target = resolve(current)
    return {"mode": current, "root": str(target["root"]),
            "requires_path_choice": bool(target["requires_path_choice"]),
            "can_select_directory": bool(config.DESKTOP_MODE and os.name == "nt")}


def save(selected, path=None, git_path=None):
    """安装成功且组件可执行之后才调用；失败或取消不得调用它覆盖上一次可用位置。"""
    if selected not in MODES:
        raise ValueError("安装模式必须是 local、custom 或 global")
    if selected == MODE_LOCAL:
        value = {"mode": MODE_LOCAL}
    elif selected == MODE_CUSTOM:
        root = custom_root(path)
        if root is None:
            raise ValueError("选择其他文件夹时缺少目标文件夹")
        value = {"mode": MODE_CUSTOM, "root": str(root)}
    else:
        if not git_path or not Path(git_path).is_absolute():
            raise ValueError("系统安装需要实际的 Git 安装位置")
        value = {"mode": MODE_GLOBAL, "git": str(git_path)}
    atomic_json(record_path(), value)
    return value
