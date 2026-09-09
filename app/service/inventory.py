"""库存操作：入库/出库/盘点调整（不可变 movement 流水）与流水查询

- 出库与盘点在同一事务内读当前库存、校验后插入流水
- 幂等键命中时返回首次提交的流水并标记 idempotent_replay
- dry_run 只校验并预览，不落库
"""
from decimal import Decimal, ROUND_HALF_UP

from .. import config, db
from ..config import now_iso
from ..errors import ApiError
from ..schemas import AdjustIn, InboundIn, OutboundIn


def _item_or_404(conn, item_id: int):
    row = conn.execute("SELECT id, name FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ApiError("ITEM_NOT_FOUND", f"单品 {item_id} 不存在",
                       hint="GET /api/v1/items 查看单品列表", status=404)
    return row


def _current_qty(conn, item_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(qty), 0) FROM movements WHERE item_id = ?",
                        (item_id,)).fetchone()[0]


def _price_to_cents(unit_price: float | None) -> int | None:
    """单价（元）→ 分，四舍五入"""
    if unit_price is None:
        return None
    return int((Decimal(str(unit_price)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _movement_out(row) -> dict:
    d = dict(row)
    d["qty_delta"] = d.pop("qty")
    if d["unit_price"] is not None:
        d["unit_price"] = float(Decimal(d["unit_price"]) / 100)
    return d


def _execute_movement(shop_id: int, *, item_id: int, mtype: str, qty_delta: int | None,
                      payload, validate=None) -> dict:
    """流水写入通用流程。validate(current) 用于出库前的库存校验。

    qty_delta 为 None 时表示盘点（由 target_qty 计算增量）。
    """
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        _item_or_404(d.conn, item_id)

        # 幂等重放：同 key 命中直接返回首次流水，不重复记账
        if payload.idempotency_key:
            first = d.conn.execute("SELECT * FROM movements WHERE idempotency_key = ?",
                                   (payload.idempotency_key,)).fetchone()
            if first is not None:
                return {"movement": _movement_out(first),
                        "current_qty": _current_qty(d.conn, item_id),
                        "idempotent_replay": True, "dry_run": False}

        current = _current_qty(d.conn, item_id)
        if qty_delta is None:  # 盘点调整：增量 = 目标 − 当前
            qty_delta = payload.target_qty - current
            if qty_delta == 0:
                raise ApiError("ADJUST_NO_CHANGE",
                               f"目标库存与当前库存相同（{current}），无需调整", status=400)
        if validate is not None:
            validate(current)

        cents = _price_to_cents(getattr(payload, "unit_price", None))
        ts = now_iso()
        movement = {"item_id": item_id, "type": mtype, "qty_delta": qty_delta,
                    "unit_price": float(Decimal(cents) / 100) if cents is not None else None,
                    "batch_no": getattr(payload, "batch_no", None),
                    "supplier": getattr(payload, "supplier", None),
                    "channel": getattr(payload, "channel", None),
                    "customer": getattr(payload, "customer", None),
                    "note": payload.note,
                    "actor": config.ACTOR, "created_at": ts}
        if qty_delta is not None and mtype == "adjust":
            movement["target_qty"] = payload.target_qty

        new_qty = current + qty_delta
        if payload.dry_run:
            return {"dry_run": True, "movement": movement,
                    "current_qty": current, "new_qty": new_qty,
                    "idempotent_replay": False}

        cur = d.conn.execute(
            "INSERT INTO movements (item_id, type, qty, unit_price, batch_no, supplier, "
            "channel, customer, note, idempotency_key, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, mtype, qty_delta, cents, movement["batch_no"], movement["supplier"],
             movement["channel"], movement["customer"], movement["note"],
             payload.idempotency_key, config.ACTOR, ts))
        movement["id"] = cur.lastrowid
        return {"movement": movement, "current_qty": new_qty,
                "idempotent_replay": False, "dry_run": False}


def inbound(shop_id: int, payload: InboundIn) -> dict:
    return _execute_movement(shop_id, item_id=payload.item_id, mtype="in",
                             qty_delta=payload.qty, payload=payload)


def outbound(shop_id: int, payload: OutboundIn) -> dict:
    def check(current: int):
        if current < payload.qty:
            raise ApiError("INSUFFICIENT_STOCK",
                           f"库存不足：当前库存 {current}，本次出库 {payload.qty}",
                           hint=f"可出库上限为 {current}；如需修正库存请使用 POST /api/v1/adjust 盘点调整",
                           status=409)

    return _execute_movement(shop_id, item_id=payload.item_id, mtype="out",
                             qty_delta=-payload.qty, payload=payload, validate=check)


def adjust(shop_id: int, payload: AdjustIn) -> dict:
    return _execute_movement(shop_id, item_id=payload.item_id, mtype="adjust",
                             qty_delta=None, payload=payload)


def list_movements(shop_id: int, item_id: int | None = None, mtype: str | None = None,
                   offset: int = 0, limit: int = 50) -> dict:
    if mtype is not None and mtype not in ("in", "out", "adjust"):
        raise ApiError("MOVEMENT_TYPE_INVALID", f"无效的流水类型：{mtype}",
                       hint="可选：in | out | adjust", status=400)
    where, params = [], []
    if item_id is not None:
        where.append("item_id = ?")
        params.append(item_id)
    if mtype is not None:
        where.append("type = ?")
        params.append(mtype)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    d = db.shop_db(shop_id)
    with d.lock:
        total = d.conn.execute("SELECT COUNT(*) FROM movements" + cond, params).fetchone()[0]
        rows = d.conn.execute(
            "SELECT * FROM movements" + cond + " ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    return {"movements": [_movement_out(r) for r in rows], "total": total}
