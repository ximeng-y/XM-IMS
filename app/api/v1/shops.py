"""店铺管理端点（无需 X-Shop-Id）"""
from fastapi import APIRouter, status

from ...schemas import ShopCreate, ShopList, ShopOut
from ...service import shops

router = APIRouter(prefix="/shops", tags=["店铺"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ShopOut,
             summary="创建店铺")
def create_shop(body: ShopCreate):
    """创建店铺：索引库登记 + 按模板生成独立店铺库；重名返回 409 SHOP_NAME_EXISTS"""
    return shops.create_shop(body.name)


@router.get("", response_model=ShopList, summary="店铺列表")
def list_shops():
    return shops.list_shops()
