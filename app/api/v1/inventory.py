"""库存操作端点（领域动作，一动作一端点）"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from ...schemas import AdjustIn, InboundIn, MovementList, MovementResult, OutboundIn
from ...service import inventory
from ..deps import require_shop

router = APIRouter(tags=["库存"])
ShopDep = Annotated[int, Depends(require_shop)]


@router.post("/inbound", status_code=status.HTTP_201_CREATED,
             response_model=MovementResult, summary="入库")
def inbound(shop_id: ShopDep, body: InboundIn):
    """入库：追加 in 流水；支持 dry_run 预览与 idempotency_key 幂等"""
    return inventory.inbound(shop_id, body)


@router.post("/outbound", status_code=status.HTTP_201_CREATED,
             response_model=MovementResult, summary="出库")
def outbound(shop_id: ShopDep, body: OutboundIn):
    """出库：库存不足返回 409 INSUFFICIENT_STOCK；支持 dry_run 与幂等键"""
    return inventory.outbound(shop_id, body)


@router.post("/adjust", status_code=status.HTTP_201_CREATED,
             response_model=MovementResult, summary="盘点调整")
def adjust(shop_id: ShopDep, body: AdjustIn):
    """盘点调整：target_qty 为目标库存（非增量），流水记 目标−当前 的带符号增量"""
    return inventory.adjust(shop_id, body)


@router.get("/movements", response_model=MovementList, summary="库存流水查询")
def list_movements(shop_id: ShopDep,
                   item_id: int | None = Query(default=None),
                   type: str | None = Query(default=None, description="in | out | adjust"),
                   offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=50, ge=1, le=200)):
    return inventory.list_movements(shop_id, item_id=item_id, mtype=type,
                                    offset=offset, limit=limit)
