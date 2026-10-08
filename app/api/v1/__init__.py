"""/api/v1 路由汇总（版本化前缀，未来规范变更时升级 v2）"""
from fastapi import APIRouter

from . import fields, inventory, items, shops, spus, update

router = APIRouter()
router.include_router(shops.router)
router.include_router(spus.router)
router.include_router(fields.router)
router.include_router(items.router)
router.include_router(inventory.router)
router.include_router(update.router)
