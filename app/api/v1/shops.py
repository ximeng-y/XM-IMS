"""店铺管理端点（无需 X-Shop-Id）"""
from fastapi import APIRouter, Query, status

from ...schemas import ShopCreate, ShopDeleteOut, ShopList, ShopOut, ShopRename, ShopRenameOut
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


@router.patch("/{shop_id}", response_model=ShopRenameOut, summary="重命名店铺")
def rename_shop(shop_id: int, body: ShopRename):
    """改名只动索引库名称，并写入该店铺库审计；ID、同步身份与创建时间保持不变。
    重名返回 409 SHOP_NAME_EXISTS，空白名返回 400 SHOP_NAME_INVALID。"""
    return shops.rename_shop(shop_id, body.name, body.dry_run, body.idempotency_key)


@router.delete("/{shop_id}", response_model=ShopDeleteOut, summary="删除整间店铺")
def delete_shop(shop_id: int,
                confirm: bool = Query(default=False, description="必须显式传 true 才会执行删除"),
                dry_run: bool = Query(default=False, description="只预览，不关闭连接、不删索引与文件"),
                idempotency_key: str | None = Query(default=None, max_length=100,
                                                    description="写操作幂等键，最长 100 字符")):
    """删除店铺及其全部所属业务数据（整间店铺库文件）。未确认返回 400 CONFIRM_REQUIRED，
    文件清理失败返回 500 SHOP_DELETE_FAILED（索引回滚，不会返回删除成功）。"""
    return shops.delete_shop(shop_id, confirm, dry_run, idempotency_key)
