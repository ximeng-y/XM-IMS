"""不可变库存流水的追加入口

库存 = SUM(movements.qty)，任何数量变化都只能追加流水，禁止 UPDATE 已有数量。
本模块只负责流水行的写入（含 sync_uid 生成），不开连接、不加锁、不提交事务、
不做库存校验：调用方必须在自己的事务内使用，并自行决定幂等、校验与返回值。
"""
from .. import db


def append_movement(conn, *, item_id: int, mtype: str, qty_delta: int,
                    unit_price: int | None = None, batch_no: str | None = None,
                    supplier: str | None = None, channel: str | None = None,
                    customer: str | None = None, note: str | None = None,
                    idempotency_key: str | None = None, actor: str,
                    created_at: str) -> int:
    """追加一条流水，返回插入后的整数 movement ID

    unit_price 为已经换算完成的分币单价（int，元 × 100），不是元的浮点值。
    """
    cur = conn.execute(
        "INSERT INTO movements (sync_uid, item_id, type, qty, unit_price, batch_no, supplier, "
        "channel, customer, note, idempotency_key, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (db.new_uid(), item_id, mtype, qty_delta, unit_price, batch_no, supplier,
         channel, customer, note, idempotency_key, actor, created_at))
    return cur.lastrowid
