"""概览统计端点（只读）"""
from typing import Annotated

from fastapi import APIRouter, Depends

from ...schemas import OverviewOut
from ...service import overview as overview_service
from ..deps import require_shop

router = APIRouter(tags=["概览"])
ShopDep = Annotated[int, Depends(require_shop)]


@router.get("/overview", response_model=OverviewOut, summary="概览统计")
def get_overview(shop_id: ShopDep):
    """返回当前店铺的库存总数量与异常条目数量。

    库存总数量为全部 SKU 当前库存的算术合计（负库存按负数计入）。
    异常条目为命中任一异常规则的 SKU 数量，条目级去重；anomalies 给出按规则细分的计数。
    当前规则：computed_field_error（任一计算字段无法计算）、negative_qty（负库存）。
    """
    return overview_service.summary(shop_id)
