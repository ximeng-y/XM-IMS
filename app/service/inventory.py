"""库存操作：入库/出库/盘点调整（不可变 movement 流水）与流水查询

- 出库与盘点在同一事务内读当前库存、校验后插入流水
- 幂等键命中时返回首次提交的流水并标记 idempotent_replay
- dry_run 只校验并预览，不落库
- 流水按 item_id（内部整数）关联；对外一律以「SPU 号-组内序号」呈现单品
"""
from decimal import Decimal, ROUND_HALF_UP

from .. import config, db
from ..config import now_iso
from ..errors import ApiError
from ..schemas import AdjustIn, InboundIn, OutboundIn
from .items import format_item_ref, resolve_item_ref


def _item_or_404(conn, item_ref: str):
    """按对外标识取单品，并带出拼显示编号所需的 spu_id/seq"""
    item_id = resolve_item_ref(conn, item_ref)
    return conn.execute("SELECT id, seq, name, spu_id FROM items WHERE id = ?",
                        (item_id,)).fetchone()


def _item_ref(conn, item_id: int) -> str:
    row = conn.execute("SELECT spu_id, seq FROM items WHERE id = ?",
                       (item_id,)).fetchone()
    return format_item_ref(row["spu_id"], row["seq"]) if row else str(item_id)


def _current_qty(conn, item_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(qty), 0) FROM movements WHERE item_id = ?",
                        (item_id,)).fetchone()[0]


def _price_to_cents(unit_price: float | None) -> int | None:
    """单价（元）→ 分，四舍五入"""
    if unit_price is None:
        return None
    return int((Decimal(str(unit_price)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _movement_out(row, ref: str) -> dict:
    d = dict(row)
    d.pop("_spu_id", None)
    d.pop("_seq", None)
    d["qty_delta"] = d.pop("qty")
    d["item_id"] = ref
    if d["unit_price"] is not None:
        d["unit_price"] = float(Decimal(d["unit_price"]) / 100)
    return d


def _execute_movement(shop_id: int, *, item_ref: str, mtype: str, qty_delta: int | None,
                      payload, validate=None) -> dict:
    """流水写入通用流程。validate(current) 用于出库前的库存校验。

    qty_delta 为 None 时表示盘点（由 target_qty 计算增量）。
    """
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        item = _item_or_404(d.conn, item_ref)
        item_id = item["id"]
        ref = format_item_ref(item["spu_id"], item["seq"])

        # 幂等重放：同 key 命中直接返回首次流水，不重复记账
        if payload.idempotency_key:
            first = d.conn.execute("SELECT * FROM movements WHERE idempotency_key = ?",
                                   (payload.idempotency_key,)).fetchone()
            if first is not None:
                return {"movement": _movement_out(first, _item_ref(d.conn, first["item_id"])),
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
        movement = {"item_id": ref, "type": mtype, "qty_delta": qty_delta,
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
    return _execute_movement(shop_id, item_ref=payload.item_id, mtype="in",
                             qty_delta=payload.qty, payload=payload)


def outbound(shop_id: int, payload: OutboundIn) -> dict:
    def check(current: int):
        if current < payload.qty:
            raise ApiError("INSUFFICIENT_STOCK",
                           f"库存不足：当前库存 {current}，本次出库 {payload.qty}",
                           hint=f"可出库上限为 {current}；如需修正库存请使用 POST /api/v1/adjust 盘点调整",
                           status=409)

    return _execute_movement(shop_id, item_ref=payload.item_id, mtype="out",
                             qty_delta=-payload.qty, payload=payload, validate=check)


def adjust(shop_id: int, payload: AdjustIn) -> dict:
    return _execute_movement(shop_id, item_ref=payload.item_id, mtype="adjust",
                             qty_delta=None, payload=payload)


def list_movements(shop_id: int, item_ref: str | None = None, mtype: str | None = None,
                   offset: int = 0, limit: int = 50) -> dict:
    if mtype is not None and mtype not in ("in", "out", "adjust"):
        raise ApiError("MOVEMENT_TYPE_INVALID", f"无效的流水类型：{mtype}",
                       hint="可选：in | out | adjust", status=400)
    d = db.shop_db(shop_id)
    with d.lock:
        where, params = [], []
        if item_ref is not None:
            where.append("m.item_id = ?")
            params.append(resolve_item_ref(d.conn, item_ref))
        if mtype:
            where.append("m.type = ?")
            params.append(mtype)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        total = d.conn.execute("SELECT COUNT(*) FROM movements m" + cond, params).fetchone()[0]
        # JOIN 出单品当前归属与序号，用于呈现对外标识（LEFT JOIN 兼容单品缺失的异常数据）
        rows = d.conn.execute(
            "SELECT m.*, i.spu_id AS _spu_id, i.seq AS _seq FROM movements m "
            "LEFT JOIN items i ON i.id = m.item_id" + cond +
            " ORDER BY m.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    movements = []
    for r in rows:
        ref = format_item_ref(r["_spu_id"], r["_seq"]) if r["_spu_id"] is not None \
            else str(r["item_id"])
        movements.append(_movement_out(r, ref))
    return {"movements": movements, "total": total}
