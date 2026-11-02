"""店铺管理：建店、列表、改名与整店删除

关于幂等：新增的改名/删除只承诺「当前进程、当前活动代次」内的结果重放，
用进程内字典实现，不新增索引表，也不跨重启保留。
"""
import logging
from pathlib import Path

from .. import config, db
from ..config import now_iso
from ..errors import ApiError

log = logging.getLogger(__name__)

# (代次, 操作, 店铺号, 幂等键) → {"request": 请求内容, "result": 首次成功结果}
_idempotency: dict[tuple, dict] = {}


def _idempotency_key(op: str, shop_id: int, key: str | None):
    return (db.epoch()[0], op, shop_id, key) if key else None


def _replay(cache_key, request):
    """命中同一键时重放首次成功结果；请求内容不同则报冲突"""
    if cache_key is None:
        return None
    cached = _idempotency.get(cache_key)
    if cached is None:
        return None
    if cached["request"] != request:
        raise ApiError(
            "IDEMPOTENCY_CONFLICT", "该幂等键已用于不同的请求内容",
            hint="同一 idempotency_key 只能重放相同请求；请换用新的键重试", status=409,
            user_message="这次操作与之前提交的内容不一致",
            user_hint="请重新发起这次操作，不要沿用之前那次的操作标识")
    return {**cached["result"], "idempotent_replay": True}


def _remember(cache_key, request, result):
    if cache_key is not None:
        _idempotency[cache_key] = {"request": request, "result": dict(result)}


def _not_found(shop_id: int) -> ApiError:
    return ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 不存在",
                    hint="GET /api/v1/shops 查看店铺列表", status=404,
                    user_message=f"店铺 {shop_id} 不存在",
                    user_hint="该店铺可能已被删除，请刷新页面后重新选择")


def _name_invalid() -> ApiError:
    return ApiError("SHOP_NAME_INVALID", "店铺名不能为空白",
                    hint="店铺名去除首尾空白后需至少 1 个字符，最长 100 个字符", status=400,
                    user_message="店铺名不能为空",
                    user_hint="请输入不超过 100 个字符的店铺名")


def _name_exists(name: str) -> ApiError:
    return ApiError("SHOP_NAME_EXISTS", f"店铺「{name}」已存在",
                    hint="店铺名全局唯一，请换一个名称", status=409,
                    user_message=f"店铺「{name}」已存在",
                    user_hint="店铺名不能重复，请换一个名称")


def create_shop(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ApiError("SHOP_NAME_INVALID", "店铺名不能为空白", status=400)
    idx = db.index_db()
    with idx.lock, idx.conn:
        if idx.conn.execute("SELECT id FROM shops WHERE name = ?", (name,)).fetchone():
            raise ApiError("SHOP_NAME_EXISTS", f"店铺「{name}」已存在",
                           hint="店铺名全局唯一，请换一个名称", status=409)
        ts = now_iso()
        # 先插入拿 id（db_file 暂空），建好库文件后回填；
        # 建文件失败时异常冒泡，索引事务回滚，不留半成品记录
        cur = idx.conn.execute(
            "INSERT INTO shops (sync_uid, name, db_file, created_at) VALUES (?, ?, '', ?)", (db.new_uid(), name, ts))
        shop_id = cur.lastrowid
        db_file = f"shops/{shop_id}.db"
        db.create_shop_db_file(shop_id, db_file)
        idx.conn.execute("UPDATE shops SET db_file = ? WHERE id = ?", (db_file, shop_id))
    return {"id": shop_id, "name": name, "created_at": ts}


def list_shops() -> dict:
    idx = db.index_db()
    with idx.lock:
        rows = idx.conn.execute(
            "SELECT id, name, created_at FROM shops ORDER BY id").fetchall()
    return {"shops": [dict(r) for r in rows]}


def rename_shop(shop_id: int, name: str, dry_run: bool = False,
                idempotency_key: str | None = None) -> dict:
    """改名：只改索引库 shops.name，并在目标店铺库记一条审计。

    店铺 ID、sync_uid、db_file、created_at 均不变；同名重复提交视为成功且不重复写审计。
    """
    name = name.strip()
    if not name:
        raise _name_invalid()
    # dry_run 既不占用也不读取写操作的结果缓存
    cache_key = None if dry_run else _idempotency_key("rename", shop_id, idempotency_key)
    with db._coordinator:
        replayed = _replay(cache_key, name)
        if replayed is not None:
            return replayed
        idx = db.index_db()
        with idx.lock, idx.conn:
            row = idx.conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if row is None:
                raise _not_found(shop_id)
            if idx.conn.execute("SELECT id FROM shops WHERE name = ? AND id <> ?",
                                (name, shop_id)).fetchone():
                raise _name_exists(name)
            result = {"id": shop_id, "name": name, "created_at": row["created_at"],
                      "dry_run": dry_run, "idempotent_replay": False}
            if dry_run:
                return result
            if name != row["name"]:
                idx.conn.execute("UPDATE shops SET name = ? WHERE id = ?", (name, shop_id))
                # 审计只能落在店铺库：索引库没有 audit_log 表
                d = db.shop_db(shop_id)
                with d.lock, d.conn:
                    db.write_audit(d.conn, "shop_rename", "shop", shop_id,
                                   {"old": row["name"], "new": name}, entity_uid=row["sync_uid"])
        _remember(cache_key, name, result)
        return result


def _shop_file_target(row) -> Path:
    """目标店铺库的物理路径：必须落在当前活动数据目录内，且是 .db 文件"""
    root = config.DATA_DIR.resolve()
    target = (config.DATA_DIR / row["db_file"]).resolve()
    if not target.is_relative_to(root) or target.suffix != ".db":
        raise ApiError("SHOP_DELETE_FAILED", f"店铺「{row['name']}」的数据文件路径不可删除：{row['db_file']}",
                       hint="索引中的 db_file 必须指向当前数据目录下的 .db 文件", status=500,
                       user_message=f"店铺「{row['name']}」的数据文件位置不正确",
                       user_hint="数据文件不在当前数据目录内，请改用备份或数据同步恢复")
    return target


def delete_shop(shop_id: int, confirm: bool = False, dry_run: bool = False,
                idempotency_key: str | None = None) -> dict:
    """整店删除：关闭连接 → 删除索引行 → 清除该店 .db 及同名 WAL/SHM。

    不逐表删除业务数据（整间店铺库随文件一起消失），也不触碰索引库、其他店铺、
    旧代次与备份。文件清理失败时整个索引事务回滚，不会返回删除成功。
    """
    cache_key = None if dry_run else _idempotency_key("delete", shop_id, idempotency_key)
    with db._coordinator:
        # 重放先于存在性检查：已删店铺的删除重试应返回首次结果
        replayed = _replay(cache_key, True)
        if replayed is not None:
            return replayed
        idx = db.index_db()
        with idx.lock, idx.conn:
            row = idx.conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if row is None:
                raise _not_found(shop_id)
            result = {"deleted": not dry_run, "shop_id": shop_id, "name": row["name"],
                      "dry_run": dry_run, "idempotent_replay": False}
            if dry_run:
                return result
            target = _shop_file_target(row)
            if not confirm:
                raise ApiError("CONFIRM_REQUIRED", "删除店铺需要显式确认",
                               hint="删除会清除该店铺的全部业务数据；确认请附带 ?confirm=true", status=400,
                               user_message="删除店铺需要再次确认",
                               user_hint="删除会清除该店铺的商品、库存流水和自定义字段等内容")
            db.close_shop(shop_id)
            idx.conn.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
            try:
                for path in (target.with_name(target.name + "-wal"),
                             target.with_name(target.name + "-shm"), target):
                    if path.exists():
                        path.unlink()
            except OSError as exc:
                # 抛错即回滚上面的索引删除；残留索引与残留文件由用户重试
                log.warning("shop delete failed: actor=%s shop=%s name=%s error=%s",
                            config.ACTOR, shop_id, row["name"], exc)
                raise ApiError("SHOP_DELETE_FAILED",
                               f"店铺「{row['name']}」的数据文件清理失败：{exc}",
                               hint="请确认没有其他程序占用该文件后重试；索引未改动", status=500,
                               user_message=f"店铺「{row['name']}」的数据文件未能清除",
                               user_hint="请关闭可能占用该文件的程序后重试") from exc
        _remember(cache_key, True, result)
        log.info("shop deleted: actor=%s shop=%s name=%s", config.ACTOR, shop_id, row["name"])
        return result
