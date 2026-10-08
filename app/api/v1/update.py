"""自动更新端点：查询检查状态、触发一键更新"""
from fastapi import APIRouter
from pydantic import BaseModel

from ...errors import ApiError
from ...service import update

router = APIRouter(prefix="/update", tags=["自动更新"])


class InstallBody(BaseModel):
    tag: str


@router.get("/status", summary="更新检查状态")
def status():
    """返回最近一次（或进行中）的更新检查结果；后台检查未完成时 checked=false。"""
    return update.check_update()


@router.post("/install", summary="安装指定版本更新")
def install(body: InstallBody):
    """tag 必须等于最近一次检查到的最新版本 tag，避免误装任意版本。"""
    st = update.check_update()
    if not st.get("checked") or not st.get("latest_tag"):
        raise ApiError(
            "UPDATE_NOT_READY",
            "更新检查尚未完成或未发现可用版本",
            hint="稍后重试，并确认远端存在 vX.Y.Z 格式的版本 tag",
        )
    if body.tag != st["latest_tag"]:
        raise ApiError(
            "TAG_MISMATCH",
            "指定的 tag 不是当前最新版本",
            hint=f"最近一次检查到的最新版本为 {st['latest_tag']}",
        )
    return update.install_update(body.tag)
