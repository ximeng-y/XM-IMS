"""两个后端入口共享的同步生命周期与业务访问门禁。"""
import asyncio
import contextlib
import time
from fastapi.responses import JSONResponse
from .. import db
from .manager import SyncManager
from .api import router


@contextlib.asynccontextmanager
async def lifespan(app):
    manager = SyncManager()
    app.state.sync = manager
    stop = asyncio.Event()

    async def loop():
        try:
            await asyncio.to_thread(manager.start)
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
                if manager.started and manager.settings["auto"] and manager.settings["remote"] and not manager.busy and not (manager.state.get("session") and not db.business_ready):
                    try:
                        await asyncio.to_thread(manager.run)
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
        if request.url.path.startswith("/api/v1/") and not request.url.path.startswith("/api/v1/sync/"):
            if not db.business_ready:
                return JSONResponse(status_code=503, content={"code": "SYNC_REQUIRED", "message": "请先完成初始化或数据同步"})
        return await call_next(request)
