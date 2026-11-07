"""将真实数据与同步门禁映射为首次使用阶段，编排第一间店铺。

同步侧状态全部经 SyncSetup 的受控视图与关键区取得，本模块只协调
「准备本机数据集 → 店铺服务创建首店」。
"""
from ..errors import ApiError
from ..schemas import FirstShopCreate
from ..service import shops
from ..sync.setup import SyncSetup


class FirstUse:
    def __init__(self, setup: SyncSetup):
        self.setup = setup

    def _local_prepare_pending(self, view):
        # 只续接纯本机初始化的存储失败；结构、远端和待处理同步状态仍走原门禁。
        return bool(view["started"] and view["active"] and not view["business_ready"]
                    and not view["remote"] and view["index_db_present"]
                    and view["status"] == "blocked"
                    and view["error_code"] == "SYNC_STORAGE_FAILED"
                    and not view["pending_work"])

    def status(self):
        view = self.setup.inspect_setup()
        result = {"phase": "waiting", "has_local_data": view["active"],
                  "shops": [], "sync": self.setup.status()}
        if not view["started"]:
            if result["sync"]["state"] == "blocked":
                result["phase"] = "sync_required"
            return result
        with self.setup.first_use_view() as ready:
            # 同步切换期间只读现有状态，不能为了查询阶段打开尚未初始化的索引库。
            if ready is None:
                return result
            if ready["active"] and ready["business_ready"] and ready["index_db_present"]:
                result["shops"] = shops.list_shops()["shops"]
                result["phase"] = "ready" if result["shops"] else "create_shop"
            elif self._local_prepare_pending(ready):
                result["phase"] = "create_shop"
            elif ready["pending_work"]:
                result["phase"] = "sync_required"
            elif not ready["active"] and ready["remote"] and result["sync"]["state"] in (
                    "initialize", "binding", "offline") or (
                    not ready["active"] and ready["remote"] and result["sync"]["state"] == "blocked"
                    and ready["error_code"] == "SYNC_STORAGE_FAILED"):
                result["phase"] = "restore_pending"
            elif not ready["active"] and not ready["remote"] and result["sync"]["state"] == "setup" \
                    and not result["sync"]["error"]:
                result["phase"] = "choose_source"
            else:
                result["phase"] = "sync_required"
        return result

    def create_shop(self, payload: FirstShopCreate):
        setup = self.setup
        if not payload.confirm and not payload.dry_run:
            raise ApiError("CONFIRM_REQUIRED", "创建首店需要 confirm=true", status=409,
                           user_message="请确认创建店铺")
        with setup.exclusive():
            view = setup.inspect_setup()
            clean_setup = not view["active"] and not view["remote"] \
                and view["status"] == "setup" and not view["has_error"] \
                and not view["pending_work"]
            usable = bool(view["active"] and view["business_ready"] and view["index_db_present"])
            prepare_pending = self._local_prepare_pending(view)
            if not (clean_setup or usable or prepare_pending):
                raise ApiError("FIRST_USE_SYNC_REQUIRED", "首店创建需要可用数据或无远端的全新 setup", status=409,
                               user_message="请先完成数据准备",
                               user_hint="进入数据同步页面继续处理")
            if (clean_setup or prepare_pending) and not payload.dry_run:
                # 初始化和首店写入分属不同文件；失败保留已建立的数据，重试读取实际店铺。
                try:
                    setup.initialize("local")
                except ApiError as exc:
                    setup.record_failure(exc, view["business_ready"])
                    raise
                usable = True
            existing = shops.list_shops()["shops"] if usable else []
            if existing:
                if len(existing) == 1 and existing[0]["name"] == payload.name:
                    return {"created": False, "shop": existing[0], "phase": "ready",
                            "sync": setup.status()}
                raise ApiError("FIRST_USE_ALREADY_READY", "已有店铺，首店接口拒绝再次创建", status=409,
                               user_message="已有可用店铺，请进入应用")
            if payload.dry_run:
                return {"created": False, "shop": None, "phase": "create_shop",
                        "sync": setup.status()}
            shop = shops.create_shop(payload.name)
            return {"created": True, "shop": shop, "phase": "ready", "sync": setup.status()}
