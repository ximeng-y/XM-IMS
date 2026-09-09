"""店铺管理：建店（索引库登记 + 按模板建店铺库）、店铺列表"""
from .. import db
from ..config import now_iso
from ..errors import ApiError


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
            "INSERT INTO shops (name, db_file, created_at) VALUES (?, '', ?)", (name, ts))
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
