"""将真实数据与同步门禁映射为首次使用阶段，编排第一间店铺。"""
from pydantic import Field, field_validator
from .. import config, db
from ..errors import ApiError
from ..schemas import ShopCreate
from ..service import shops


class FirstShopCreate(ShopCreate):
    confirm: bool = False
    idempotency_key: str = Field(min_length=1, max_length=100)
    dry_run: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("店铺名称需包含有效文字")
        return value


class FirstUse:
    def __init__(self, manager):
        self.manager = manager

    def _local_prepare_pending(self):
        manager = self.manager
        # 只续接纯本机初始化的存储失败；结构、远端和待处理同步状态仍走原门禁。
        return bool(manager.started and manager.active and not db.business_ready
                    and not manager.settings["remote"] and config.INDEX_DB.is_file()
                    and manager.status_name == "blocked"
                    and (manager.error or {}).get("code") == "SYNC_STORAGE_FAILED"
                    and not any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")))

    def status(self):
        manager = self.manager
        sync = manager.status()
        result = {"phase": "waiting", "has_local_data": bool(manager.active),
                  "shops": [], "sync": sync}
        if not manager.started:
            if sync["state"] == "blocked":
                result["phase"] = "sync_required"
            return result
        # 同步切换期间只读现有状态，不能为了查询阶段打开尚未初始化的索引库。
        if not manager.mutex.acquire(blocking=False):
            return result
        try:
            if manager.busy:
                return result
            with db._coordinator:
                if manager.active and db.business_ready and config.INDEX_DB.is_file():
                    result["shops"] = shops.list_shops()["shops"]
                    result["phase"] = "ready" if result["shops"] else "create_shop"
                elif self._local_prepare_pending():
                    result["phase"] = "create_shop"
                elif any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
                    result["phase"] = "sync_required"
                elif not manager.active and manager.settings["remote"] and (
                        sync["state"] in ("initialize", "binding", "offline") or
                        (sync["state"] == "blocked" and (sync["error"] or {}).get("code") == "SYNC_STORAGE_FAILED")):
                    result["phase"] = "restore_pending"
                elif not manager.active and not manager.settings["remote"] and sync["state"] == "setup" and not sync["error"]:
                    result["phase"] = "choose_source"
                else:
                    result["phase"] = "sync_required"
        finally:
            manager.mutex.release()
        return result

    def create_shop(self, payload: FirstShopCreate):
        manager = self.manager
        if not payload.confirm and not payload.dry_run:
            raise ApiError("CONFIRM_REQUIRED", "创建首店需要 confirm=true", status=409,
                           user_message="请确认创建店铺")
        with manager.operation(), db._coordinator:
            clean_setup = not manager.active and not manager.settings["remote"] \
                and manager.status_name == "setup" and not manager.error \
                and not any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution"))
            usable = bool(manager.active and db.business_ready and config.INDEX_DB.is_file())
            prepare_pending = self._local_prepare_pending()
            if not (clean_setup or usable or prepare_pending):
                raise ApiError("FIRST_USE_SYNC_REQUIRED", "首店创建需要可用数据或无远端的全新 setup", status=409,
                               user_message="请先完成数据准备",
                               user_hint="进入数据同步页面继续处理")
            if (clean_setup or prepare_pending) and not payload.dry_run:
                # 初始化和首店写入分属不同文件；失败保留已建立的数据，重试读取实际店铺。
                try:
                    manager._initialize_body("local")
                except ApiError as exc:
                    manager._record_initialize_failure(exc, False)
                    raise
                usable = True
            existing = shops.list_shops()["shops"] if usable else []
            if existing:
                if len(existing) == 1 and existing[0]["name"] == payload.name:
                    return {"created": False, "shop": existing[0], "phase": "ready", "sync": manager.status()}
                raise ApiError("FIRST_USE_ALREADY_READY", "已有店铺，首店接口拒绝再次创建", status=409,
                               user_message="已有可用店铺，请进入应用")
            if payload.dry_run:
                return {"created": False, "shop": None, "phase": "create_shop", "sync": manager.status()}
            shop = shops.create_shop(payload.name)
            return {"created": True, "shop": shop, "phase": "ready", "sync": manager.status()}
