"""应用级装配：生命周期、服务对象与请求门禁。

两个后端入口（app.main、app.api_app）共用这里的生命周期；同步相关路由
由 app.api.v1 统一注册，本模块不再挂载路由。
"""
import asyncio
import contextlib
import time

from fastapi.responses import JSONResponse

from . import db
from .errors import ApiError, error_dict
from .onboarding.backup import BackupOnboarding
from .onboarding.first_use import FirstUse
from .service import update
from .sync.manager import SyncManager
from .sync.setup import SyncSetup


@contextlib.asynccontextmanager
async def lifespan(app):
    update.require_complete_installation()
    # 两个后端入口共用；仅在应用真正启动时检查，模块级锁防止重复线程。
    update.start_background_check()
    manager = SyncManager()
    setup = SyncSetup(manager)
    app.state.sync = manager
    app.state.sync_setup = setup
    app.state.onboarding_backup = BackupOnboarding(manager)
    app.state.onboarding_first_use = FirstUse(setup)
    stop = asyncio.Event()

    async def loop():
        try:
            await asyncio.to_thread(manager.start)
        except ApiError as exc:
            manager.block_start(exc)
        except Exception:
            manager.block_start(error_dict(
                "SYNC_START_FAILED", "同步初始化失败，请检查数据目录或重复运行的实例",
                user_message="数据同步没有启动成功",
                user_hint="请确认程序没有重复打开，且数据目录可正常读写后重启程序"))
        # 本地备份与上传云端各有一份周期基准，互不影响。
        now = time.monotonic()
        last_local_run = last_cloud_run = now
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            window = manager.background_window()
            now = time.monotonic()
            local_due = now - last_local_run >= window["local_interval"] * 60
            cloud_due = now - last_cloud_run >= window["cloud_interval"] * 60
            if local_due or cloud_due:
                runnable = not update.maintenance_error() and window["started"] \
                    and not window["busy"] and not window["session_open"]
                # 到期后无论成功、失败还是因开关或门禁跳过，都只推进这一阶段自己的计时。
                if local_due:
                    last_local_run = now
                if cloud_due:
                    last_cloud_run = now
                if local_due and window["local_auto"]:
                    if runnable:
                        try:
                            await asyncio.to_thread(manager.background_local_backup)
                        except ApiError as exc:
                            manager.record_local_error(exc)
                        except Exception:
                            manager.record_local_error(error_dict(
                                "BACKUP_FAILED", "后台本地备份未完成，请进入同步页重试",
                                user_message="后台备份没有完成",
                                user_hint="请进入数据同步页面点击「立即备份」重试"))
                if cloud_due and window["auto"] and window["remote"]:
                    # 自动上传只发送已保存的本地备份候选，不因到期而补做本地备份。
                    if runnable:
                        try:
                            await asyncio.to_thread(manager.run)
                        except ApiError as exc:
                            manager.record_background_error(exc)
                        except Exception:
                            manager.record_background_error(error_dict(
                                "SYNC_CHECK_FAILED", "后台同步未完成，请进入同步页重试",
                                user_message="后台同步没有完成",
                                user_hint="请进入同步页面点击「立即同步」重试"))
    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        stop.set()
        await task
        await asyncio.to_thread(manager.stop)


def install(app):
    """安装请求门禁；路由已由 app.api.v1 统一注册，这里不再挂载。"""
    @app.middleware("http")
    async def guard(request, call_next):
        # 更新响应仍可返回；其他业务、同步操作和单端口静态请求暂停到恢复/重启。
        if request.url.path not in ("/api/v1/update/status", "/api/v1/update/install"):
            error = update.maintenance_error()
            if error:
                return JSONResponse(status_code=503, content=error)
        if request.url.path.startswith("/api/v1/") and not request.url.path.startswith("/api/v1/sync/"):
            if not db.business_ready and not request.url.path.startswith("/api/v1/update/"):
                return JSONResponse(status_code=503, content=error_dict(
                    "SYNC_REQUIRED", "请先完成初始化或数据同步",
                    user_message="程序还没有完成初始化或数据同步",
                    user_hint="请先完成初始化或数据同步后再继续"))
        return await call_next(request)
