"""自动更新端点：查询检查状态、触发一键更新"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ...errors import ApiError
from ...service import update
from ..access import update_request


def local_update_request(request: Request):
    """在直连后端和前端代理边界使用同一访问规则。"""
    update_request(request)


router = APIRouter(prefix="/update", tags=["自动更新"],
                   dependencies=[Depends(local_update_request)])


class InstallBody(BaseModel):
    tag: str


class UpdateSettingsBody(BaseModel):
    auto_check: bool


@router.get("/status", summary="更新检查状态")
def status(force: bool = False):
    """返回最近一次（或进行中）的更新检查结果；后台检查未完成时 checked=false。

    force=true 时忽略缓存立即向远端检查（手动「检查更新」按钮使用，可能耗时数十秒）。
    """
    return update.check_update(force=force)


@router.get("/settings", summary="读取更新检查设置")
def get_settings():
    return update.load_settings()


@router.patch("/settings", summary="保存更新检查设置")
def patch_settings(body: UpdateSettingsBody):
    """auto_check 控制程序启动时是否自动检查新版本；手动检查不受影响。"""
    return update.save_settings({"auto_check": body.auto_check})


@router.post("/install", summary="安装指定版本更新")
def install(body: InstallBody, request: Request):
    """tag 必须等于最近一次检查到的最新版本 tag，避免误装任意版本。"""
    manager = getattr(request.app.state, "sync", None)
    if manager is None or not manager.started:
        raise ApiError("UPDATE_NOT_READY", "本实例尚未取得独占访问权，不能安装更新", status=503)
    if manager.busy:
        raise ApiError("UPDATE_BUSY", "数据同步正在进行，请完成后再更新程序", status=409)
    st = update.check_update()
    if not st.get("checked") or not st.get("latest_tag"):
        raise ApiError(
            "UPDATE_NOT_READY",
            "更新检查尚未完成或未发现可用版本",
            hint="稍后重试，并确认远端存在 vX.Y.Z 格式的版本 tag",
            user_message="暂时无法确认是否有新版本",
            user_hint="请稍后重试",
        )
    if body.tag != st["latest_tag"]:
        raise ApiError(
            "TAG_MISMATCH",
            "指定的 tag 不是当前最新版本",
            hint=f"最近一次检查到的最新版本为 {st['latest_tag']}",
            user_message="要安装的版本与当前最新版本不一致",
            user_hint=f"请重新检查更新后再安装（当前最新版本为 {st['latest_tag']}）",
        )
    return update.install_update(body.tag)
