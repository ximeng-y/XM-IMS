"""备份配置引导端点（URL 仍以 /sync/onboarding 开头）。

环境检测与候选检查只读、不占用同步任务锁；安装、显式登录与最终应用
在后端互斥执行。
"""
from fastapi import APIRouter, Request, Body, Query

from ..access import require_sync_manager
from ...errors import ApiError
from ...schemas import FirstShopCreate

router = APIRouter(prefix="/sync/onboarding", tags=["数据同步"])


@router.get("/environment")
def onboarding_environment(request: Request):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.environment()


@router.post("/install")
def onboarding_install(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise ApiError("CONFIRM_REQUIRED", "请确认要安装的组件", status=409,
                       user_message="请先确认安装操作",
                       user_hint="安装会从官方来源下载并运行安装包，需要明确确认")
    require_sync_manager(request)
    # mode / path 为可选增量：源码模式不传时保持原有安装实现。
    # path 是原生窗口选中的父目录，仅 custom 模式使用，服务端再拼专用子目录。
    mode, path = body.get("mode"), body.get("path")
    return request.app.state.onboarding_backup.install(
        body.get("component"),
        mode=mode if isinstance(mode, str) else None,
        path=path if isinstance(path, str) else None)


@router.get("/github")
def onboarding_github(request: Request, account: str | None = Query(default=None)):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.github_status(account)


@router.post("/github/login")
def onboarding_github_login(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise ApiError("CONFIRM_REQUIRED", "请确认要进行的浏览器授权", status=409,
                       user_message="请先确认登录操作",
                       user_hint="登录会打开系统浏览器完成 GitHub 授权，需要明确确认")
    require_sync_manager(request)
    return request.app.state.onboarding_backup.github_login(body.get("account"), body.get("force") is True)


@router.post("/check")
def onboarding_check(request: Request, body: dict = Body(...)):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.check(body)


@router.post("/complete")
def onboarding_complete(request: Request, body: dict = Body(...)):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.complete(body)


@router.get("/first-use/status")
def first_use_status(request: Request):
    require_sync_manager(request)
    return request.app.state.onboarding_first_use.status()


@router.post("/first-use/shop")
def first_use_shop(request: Request, body: FirstShopCreate):
    require_sync_manager(request)
    return request.app.state.onboarding_first_use.create_shop(body)


@router.post("/flow/check")
def onboarding_flow_check(request: Request, body: dict = Body(...)):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.check_flow(body)


@router.post("/flow/complete")
def onboarding_flow_complete(request: Request, body: dict = Body(...)):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.complete_flow(body)


@router.post("/flow/resume")
def onboarding_flow_resume(request: Request, body: dict = Body(...)):
    require_sync_manager(request)
    return request.app.state.onboarding_backup.resume_flow(body)
