"""/api/v1 路由汇总（版本化前缀，未来规范变更时升级 v2）

普通业务路由在前，同步管理与备份引导路由在后；三者各自注册一次。
"""
from fastapi import APIRouter

from . import (fields, inventory, items, onboarding, overview, shops, spus, sync, update,
               view_settings)

router = APIRouter()
router.include_router(shops.router)
router.include_router(spus.router)
router.include_router(fields.router)
router.include_router(items.router)
router.include_router(inventory.router)
router.include_router(overview.router)
router.include_router(view_settings.router)
router.include_router(update.router)
router.include_router(sync.router)
router.include_router(onboarding.router)
