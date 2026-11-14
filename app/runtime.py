"""应用级装配：生命周期、服务对象与请求门禁。

两个后端入口（app.main、app.api_app）共用这里的生命周期；同步相关路由
由 app.api.v1 统一注册，本模块不再挂载路由。

桌面首次启动：backend_runner 注入本次控制对象后进入试运行分支——先在本机副本上
完成复制与迁移，等外壳提交数据指针，再等放行命令进入正常同步启动。其他启动完全
走原有分支。
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

# 本次桌面首次启动的控制对象；由 backend_runner 在应用装配前注入
_trial_control = None
# 试运行写入门禁：从进入试运行到放行成功都阻断业务写入
_trial_active = False


def set_trial_control(control):
    global _trial_control, _trial_active
    _trial_control = control
    _trial_active = control is not None


def trial_control():
    return _trial_control


async def _trial(manager, control):
    """第一段：副本迁移 → 等提交 → 等放行，之后交回正常后台周期。"""
    global _trial_active

    def progress(done, total):
        control.report("copying", f"{done} {total}")

    try:
        await asyncio.to_thread(manager.prepare_trial, control.attempt, progress)
    except ApiError as exc:
        manager.block_start(exc)
        control.report("failed", "data")
        return False
    except Exception:
        manager.block_start(error_dict(
            "SYNC_START_FAILED", "同步初始化失败，请检查数据目录或重复运行的实例",
            user_message="数据同步没有启动成功",
            user_hint="请确认程序没有重复打开，且数据目录可正常读写后重启程序"))
        control.report("failed", "data")
        return False
    control.report("local_ready")
    if not await asyncio.to_thread(control.wait_commit):
        return False
    try:
        await asyncio.to_thread(manager.commit_trial, control.attempt)
    except Exception:
        control.report("failed", "commit")
        return False
    control.report("data_committed")
    if not await asyncio.to_thread(control.wait_release):
        return False
    try:
        await asyncio.to_thread(manager.release_trial)
    except ApiError as exc:
        manager.block_start(exc)
        control.report("failed", "release")
        return False
    except Exception:
        manager.block_start(error_dict(
            "SYNC_START_FAILED", "同步初始化失败，请检查数据目录或重复运行的实例",
            user_message="数据同步没有启动成功",
            user_hint="请确认程序没有重复打开，且数据目录可正常读写后重启程序"))
        control.report("failed", "release")
        return False
    # 放行成功后恢复普通启动行为：不再阻断业务写入
    _trial_active = False
    control.report("released")
    return True


async def _background(manager, stop):
    """正常后台周期：本地备份与上传云端各有一份周期基准，互不影响。"""
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
            # 每个阶段在自己结束后重新计时；成功、失败或跳过都一样推进，只影响自己的间隔。
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
            if local_due:
                last_local_run = time.monotonic()
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
            if cloud_due:
                last_cloud_run = time.monotonic()


async def _run(manager, stop):
    control = _trial_control
    if control is not None:
        if not await _trial(manager, control):
            return
    else:
        try:
            await asyncio.to_thread(manager.start)
        except ApiError as exc:
            manager.block_start(exc)
        except Exception:
            manager.block_start(error_dict(
                "SYNC_START_FAILED", "同步初始化失败，请检查数据目录或重复运行的实例",
                user_message="数据同步没有启动成功",
                user_hint="请确认程序没有重复打开，且数据目录可正常读写后重启程序"))
    await _background(manager, stop)


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
    task = asyncio.create_task(_run(manager, stop))
    try:
        yield
    finally:
        stop.set()
        if _trial_control is not None:
            _trial_control.stop()
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
        # 首次启动确认前不开放业务写入：页面与只读设置仍可加载，同步状态与
        # 本机更新设置除外；其他写请求一律拒绝
        if _trial_active and request.method not in ("GET", "HEAD", "OPTIONS") \
                and not request.url.path.startswith(("/api/v1/sync/", "/api/v1/update/settings")):
            return JSONResponse(status_code=503, content=error_dict(
                "UPDATE_TRIAL", "程序正在确认新版本，暂不能修改数据",
                user_message="正在确认新版本，暂时不能修改数据",
                user_hint="请稍候，确认完成后即可正常使用"))
        if request.url.path.startswith("/api/v1/") and not request.url.path.startswith("/api/v1/sync/"):
            if not db.business_ready and not request.url.path.startswith("/api/v1/update/"):
                return JSONResponse(status_code=503, content=error_dict(
                    "SYNC_REQUIRED", "请先完成初始化或数据同步",
                    user_message="程序还没有完成初始化或数据同步",
                    user_hint="请先完成初始化或数据同步后再继续"))
        return await call_next(request)
