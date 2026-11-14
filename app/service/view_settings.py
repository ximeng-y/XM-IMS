"""页面列设置的本机存储：按店铺稳定身份与页面标识分别保存

设置文件放在持久数据根（DATA_ROOT）下的 ui/preferences.json，不属于任何数据库代次：
重启、访问端口变化或代次切换都不影响。这里只维护列设置自身的读写契约，
不复制前端列文案、列宽规则或业务渲染逻辑。

存储结构：
{"shops": {"<sync_uid>": {"<view>": {"order": [...], "hidden": [...], "sidebar_width": 360}}}}
单次保存只替换目标店铺、目标页面的记录，其余记录原样保留。
"""
from pathlib import Path
import threading

from .. import db
from ..config import DATA_ROOT
from ..errors import ApiError
from ..storage.files import atomic_json, read_json

VIEWS = ("spus", "items", "inventory")

# 读—改—写必须是一个整体：两个标签页几乎同时保存时，后一次读取若发生在
# 前一次写入之前，就会把对方刚写入的其他页面设置覆盖掉。
# 写入只发生在后端进程内（单端口直连或前端代理转发到同一后端），进程内互斥即可。
_lock = threading.Lock()


def _path() -> Path:
    return DATA_ROOT / "ui" / "preferences.json"


def _unreadable(detail: str) -> ApiError:
    return ApiError(
        "VIEW_SETTINGS_UNREADABLE", f"页面设置文件无法读取：{detail}", status=500,
        hint="可备份并移走 data/ui/preferences.json 后重试；程序不会自动覆盖已有文件",
        user_message="读取页面设置失败",
        user_hint="请稍后重试；若一直失败，请检查程序数据目录下 ui/preferences.json 是否损坏")


def _read_all() -> dict:
    """读取整个设置文件；文件或目录缺失视为没有记录，已有文件读不出来则报错"""
    path = _path()
    if not path.exists():
        return {}
    try:
        data = read_json(path, {})
    except (OSError, ValueError) as exc:
        raise _unreadable(str(exc))
    if not isinstance(data, dict):
        raise _unreadable("顶层结构不是对象")
    shops = data.get("shops")
    if shops is not None and not isinstance(shops, dict):
        raise _unreadable("shops 不是对象")
    return data


def _normalize(record) -> dict:
    """任何来源的记录 → 规范设置；未知类型逐项丢弃，不因一条脏数据报错"""
    if not isinstance(record, dict):
        return {"order": [], "hidden": [], "sidebar_width": None}
    order = [x for x in record.get("order") or [] if isinstance(x, str) and x]
    hidden = [x for x in record.get("hidden") or [] if isinstance(x, str) and x]
    width = record.get("sidebar_width")
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        width = None
    return {"order": list(dict.fromkeys(order)),
            "hidden": list(dict.fromkeys(hidden)),
            "sidebar_width": width}


def _shop_uid(shop_id: int) -> str:
    """稳定店铺身份：业务请求仍用整数 ID，落盘改用 sync_uid，店铺改号不影响设置"""
    row = db.get_shop_row(shop_id)
    if row is None:
        raise ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 不存在",
                       hint="GET /api/v1/shops 查看店铺列表", status=404)
    return row["sync_uid"]


def load(shop_id: int, view: str) -> dict:
    """读取指定店铺、指定页面的设置；没有记录时返回空顺序、空隐藏与默认宽度"""
    uid = _shop_uid(shop_id)
    shops = _read_all().get("shops") or {}
    record = (shops.get(uid) or {}).get(view)
    return _normalize(record)


def save(shop_id: int, view: str, values: dict) -> dict:
    """整体替换目标店铺、目标页面的记录并落盘，返回保存后的完整设置"""
    uid = _shop_uid(shop_id)
    with _lock:
        data = _read_all()
        shops = data.setdefault("shops", {})
        if not isinstance(shops.get(uid), dict):
            shops[uid] = {}
        saved = _normalize(values)
        shops[uid][view] = saved
        try:
            atomic_json(_path(), data)
        except OSError as exc:
            raise ApiError("VIEW_SETTINGS_WRITE_FAILED", f"页面设置写入失败：{exc}", status=500,
                           user_message="保存页面设置失败",
                           user_hint="请确认程序数据目录可写后重试")
        return saved
