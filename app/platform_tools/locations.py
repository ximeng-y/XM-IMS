"""依赖（Git / Git Credential Manager）安装位置的唯一实现。

记录文件为 DATA_ROOT/desktop/tools.json，只有本模块读写它：

    {"mode": "local"}                            默认局部安装，根为安装根 tools
    {"mode": "custom", "root": "<所选文件夹>/XM-IMS-tools"}
    {"mode": "global", "git": "<实际检测到的系统 Git 目录>"}
    {"mode": "local", "gcm": {"mode": "local"}}  独立的局部登录组件目标

local 不保存绝对根：软件目录可能被整体移动，跟着安装根解析才不会失效。
custom 保存最终完整根，界面展示的就是它。global 只在用户明确选择系统安装
并安装成功后才写入，且写的是实际检测到的位置，不是「已启动安装器」。

gcm 子项只记录独立的局部 / 自选登录组件目标，不覆盖顶层 Git 选择：复用系统
Git 时也可以在软件目录或自选目录单独安装登录组件。缺少该子项的老记录不迁移，
按原共同目标（局部 / 自选 Git 所在根）解析登录组件。

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

COMPONENT_GIT = "git"
COMPONENT_GCM = "gcm"

# 自选文件夹下使用专用子目录，不把用户选中的整个文件夹当作工具根
CUSTOM_DIR_NAME = "XM-IMS-tools"

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


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


def configured_mode():
    """明确的 Git 位置选择；没有有效记录时返回 None，不代之以 local。

    文件里可能只有独立的登录组件目标，那不算对 Git 的选择。
    """
    value = _record().get("mode")
    return value if value in MODES else None


def _gcm_record():
    """独立的登录组件目标子项；没有或损坏时返回空字典。"""
    value = _record().get("gcm")
    return value if isinstance(value, dict) and value.get("mode") in MODES else {}


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


def gcm_target():
    """独立登录组件的目标 {mode, root, gcm_root}，root 为模式级依赖根。

    - 有 gcm 子项：按子项的模式解析；
    - 没有子项且 Git 明确为自选：沿用原共同目标的 gcm 目录；
    - 没有子项且 Git 为局部 / 系统 / 未配置：使用软件专用目录。
    """
    saved = _gcm_record()
    if saved:
        if saved["mode"] == MODE_CUSTOM:
            root = custom_root(saved["root"])
            if root is not None:
                return {"mode": MODE_CUSTOM, "root": root, "gcm_root": root / "gcm"}
        return {"mode": MODE_LOCAL, "root": local_root(), "gcm_root": local_root() / "gcm"}
    if configured_mode() == MODE_CUSTOM:
        root = custom_root()
        if root is not None:
            return {"mode": MODE_CUSTOM, "root": root, "gcm_root": root / "gcm"}
    return {"mode": MODE_LOCAL, "root": local_root(), "gcm_root": local_root() / "gcm"}


def _custom_base(component, path):
    """自选模式的目标依赖根；独立登录组件优先用自己的子项记录。"""
    if path is None and component == COMPONENT_GCM:
        target = gcm_target()
        return target["root"] if target["mode"] == MODE_CUSTOM else None
    return custom_root(path)


def resolve(target_mode=None, path=None, component=COMPONENT_GIT):
    """本次安装的目标描述；不改记录，不检查组件是否可执行。

    component 默认为 Git；GCM 使用独立子项或老记录共同目标，不改变 Git 顶层选择。
    """
    if component not in (COMPONENT_GIT, COMPONENT_GCM):
        raise ValueError("组件必须是 git 或 gcm")
    target_mode = target_mode or mode()
    if target_mode not in MODES:
        raise ValueError("安装模式必须是 local、custom 或 global")
    if target_mode == MODE_GLOBAL:
        return {"mode": MODE_GLOBAL, "root": global_git(), "git_root": None,
                "gcm_root": None, "requires_path_choice": False}
    root = local_root() if target_mode == MODE_LOCAL else _custom_base(component, path)
    if root is None:
        # 自选模式缺少父目录属于请求缺项，不能悄悄换成局部目标
        raise ValueError("选择其他文件夹时缺少目标文件夹")
    return {"mode": target_mode, "root": root,
            "git_root": git_root(target_mode, path), "gcm_root": root / "gcm",
            "requires_path_choice": has_chinese(root)}


def view(component=COMPONENT_GIT):
    """界面需要的当前目标信息；不暴露记录里的其它内容。"""
    if component == COMPONENT_GCM:
        target = gcm_target()
        return {"mode": target["mode"], "root": str(target["root"]),
                "requires_path_choice": has_chinese(target["root"]),
                "can_select_directory": bool(config.DESKTOP_MODE and os.name == "nt")}
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
    """安装成功且组件可执行之后才调用；失败或取消不得调用它覆盖上一次可用位置。

    独立的登录组件子项与 Git 选择互不覆盖：写 Git 时保留已有 gcm 子项。
    """
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
    saved = _gcm_record()
    if saved:
        value["gcm"] = saved
    atomic_json(record_path(), value)
    return value


def save_gcm(selected, path=None):
    """独立登录组件安装成功后保存其目标；不覆盖顶层 Git 选择。

    系统位置沿用「随系统 Git 自带」，不在这里写入新的全局 GCM 位置。
    """
    if selected not in (MODE_LOCAL, MODE_CUSTOM):
        raise ValueError("独立安装登录组件只支持 local 或 custom 模式")
    value = {"mode": MODE_LOCAL}
    if selected == MODE_CUSTOM:
        root = custom_root(path)
        if root is None:
            raise ValueError("选择其他文件夹时缺少目标文件夹")
        value = {"mode": MODE_CUSTOM, "root": str(root)}
    record = dict(_record())
    record["gcm"] = value
    atomic_json(record_path(), record)
    return value
