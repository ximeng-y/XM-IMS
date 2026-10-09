"""两个后端入口共享的同步生命周期与业务访问门禁。"""
import asyncio
import contextlib
import time
from fastapi.responses import JSONResponse
from .. import db
from ..errors import ApiError
from ..service import update
from .manager import SyncManager
from .api import router


@contextlib.asynccontextmanager
async def lifespan(app):
    update.require_complete_installation()
    # 两个后端入口共用；仅在应用真正启动时检查，模块级锁防止重复线程。
    update.start_background_check()
    manager = SyncManager()
    app.state.sync = manager
    stop = asyncio.Event()

    async def loop():
        try:
            await asyncio.to_thread(manager.start)
        except ApiError as exc:
            manager.status_name = "blocked"
            manager.error = manager._error_entry(exc)
            db.set_ready(False)
        except Exception:
            manager.status_name = "blocked"
            manager.error = {"code": "SYNC_START_FAILED", "message": "同步初始化失败，请检查数据目录或重复运行的实例"}
            db.set_ready(False)
        last_run = time.monotonic()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last_run >= manager.settings["interval"] * 60:
                if not update.maintenance_error() and manager.started and manager.settings["auto"] and manager.settings["remote"] and not manager.busy and not (manager.state.get("session") and not db.business_ready):
                    try:
                        await asyncio.to_thread(manager.run)
                    except ApiError as exc:
                        manager.error = manager._error_entry(exc)
                    except Exception:
                        manager.error = {"code": "SYNC_CHECK_FAILED", "message": "后台同步未完成，请进入同步页重试"}
                last_run = time.monotonic()
    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        stop.set()
        await task
        await asyncio.to_thread(manager.stop)


def install(app):
    app.include_router(router, prefix="/api/v1")

    @app.middleware("http")
    async def guard(request, call_next):
        # 更新响应仍可返回；其他业务、同步操作和单端口静态请求暂停到恢复/重启。
        if request.url.path not in ("/api/v1/update/status", "/api/v1/update/install"):
            error = update.maintenance_error()
            if error:
                return JSONResponse(status_code=503, content=error)
        if request.url.path.startswith("/api/v1/") and not request.url.path.startswith("/api/v1/sync/"):
            if not db.business_ready and not request.url.path.startswith("/api/v1/update/"):
                return JSONResponse(status_code=503, content={"code": "SYNC_REQUIRED", "message": "请先完成初始化或数据同步"})
        return await call_next(request)
