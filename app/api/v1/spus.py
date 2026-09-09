"""SPU（顶层分类）端点"""
from typing import Annotated

from fastapi import APIRouter, Depends, status

from ...schemas import SpuCreate, SpuList, SpuOut
from ...service import spus
from ..deps import require_shop

router = APIRouter(prefix="/spus", tags=["SPU"])
ShopDep = Annotated[int, Depends(require_shop)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SpuOut,
             summary="创建 SPU")
def create_spu(shop_id: ShopDep, body: SpuCreate):
    return spus.create_spu(shop_id, body.name)


@router.get("", response_model=SpuList, summary="SPU 列表（含 SKU 计数）")
def list_spus(shop_id: ShopDep):
    return spus.list_spus(shop_id)


@router.patch("/{spu_id}", response_model=SpuOut, summary="SPU 改名（全局一处生效）")
def rename_spu(shop_id: ShopDep, spu_id: int, body: SpuCreate):
    return spus.rename_spu(shop_id, spu_id, body.name)
