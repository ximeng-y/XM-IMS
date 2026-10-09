"""单品（SKU）端点"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from ...schemas import ItemCreate, ItemCreatedOut, ItemList, ItemOut, ItemPatch
from ...service import items
from ..deps import require_shop

router = APIRouter(prefix="/items", tags=["单品"])
ShopDep = Annotated[int, Depends(require_shop)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ItemCreatedOut,
             summary="创建单品（SKU）")
def create_item(shop_id: ShopDep, body: ItemCreate):
    """spu_id/spu_name 二选一必填；spu_name 精确匹配已有 SPU 则复用，否则自动创建。
    values 按自定义字段名传值（如 {"售价": 12.34}）。"""
    return items.create_item(shop_id, body)


@router.get("", response_model=ItemList, summary="单品列表")
def list_items(shop_id: ShopDep,
               q: str | None = Query(default=None, description="SKU 名子串模糊搜索"),
               name: str | None = Query(default=None,
                                        description="SKU 名精确匹配（创建前查重用，与 spu_id 配合）"),
               spu_id: int | None = Query(default=None),
               offset: int = Query(default=0, ge=0),
               limit: int = Query(default=50, ge=1, le=200)):
    return items.list_items(shop_id, offset=offset, limit=limit, q=q, spu_id=spu_id, name=name)


@router.get("/{item_id}", response_model=ItemOut, summary="单品详情")
def get_item(shop_id: ShopDep, item_id: str):
    """item_id 为「SPU 号-组内序号」，如 1-2。"""
    return items.get_item(shop_id, item_id)


@router.patch("/{item_id}", response_model=ItemOut, summary="修改单品（改名/换挂 SPU/改字段值）")
def patch_item(shop_id: ShopDep, item_id: str, body: ItemPatch):
    """改价即修改价格类字段值，审计记录 old/new。
    values 中某字段传 null 表示清除该字段值。
    换挂 SPU 后编号变为「新 SPU 号-新序号」（落在目标 SPU 组内末尾）。"""
    return items.patch_item(shop_id, item_id, body)
