"""自定义字段端点"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from ...schemas import FieldCreate, FieldList, FieldOut, FieldConvert, FieldRename
from ...service import fields
from ..deps import require_shop

router = APIRouter(prefix="/fields", tags=["自定义字段"])
ShopDep = Annotated[int, Depends(require_shop)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FieldOut,
             summary="创建自定义字段")
def create_field(shop_id: ShopDep, body: FieldCreate):
    """创建自定义字段；与系统字段（ID/名称/库存数量/SPU ID/SPU 名称）重名返回 409"""
    return fields.create_field(shop_id, body.name, body.type,
                               body.allow_negative, body.scale)


@router.get("", response_model=FieldList, summary="字段列表（5 个系统字段 + 自定义字段）")
def list_fields(shop_id: ShopDep):
    return fields.list_fields(shop_id)


@router.patch("/{field_id}", response_model=FieldOut, summary="字段改名（数据不动）")
def rename_field(shop_id: ShopDep, field_id: int, body: FieldRename):
    return fields.rename_field(shop_id, field_id, body.name)


@router.post("/{field_id}/convert", response_model=None, summary="转换字段结构")
def convert_field(shop_id: ShopDep, field_id: int, body: FieldConvert) -> Any:
    """转换字段结构，返回转换报告：
    {description, lossy, affected_items, samples: [{item_id, item_name, old, new}]}。

    - string/bool 与其他类型互转需 confirm_clear=true（清空全部值）
    - 有损转换必须带 rounding（ceil/floor/round），缺失返回 400 ROUNDING_REQUIRED
    - dry_run=true 只返回报告不落库
    """
    return fields.convert_field(shop_id, field_id, body.new_type, body.new_scale,
                                body.new_allow_negative, body.rounding,
                                body.confirm_clear, body.dry_run)


@router.delete("/{field_id}", response_model=None, summary="删除字段")
def delete_field(shop_id: ShopDep, field_id: int,
                 confirm: bool = Query(default=False,
                                       description="必须显式传 true 才会执行删除")) -> Any:
    """删除自定义字段及其全部值（级联），返回 {deleted, field, deleted_values}"""
    return fields.delete_field(shop_id, field_id, confirm)
