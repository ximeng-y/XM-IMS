"""仅本机可访问的同步管理接口。"""
import ipaddress
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, Body, Query
from ..errors import ApiError

router = APIRouter(prefix="/sync", tags=["数据同步"])


def local_request(request, *, scope="SYNC", header="x-xm-sync", label="同步管理"):
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    hostname = urlsplit("http://" + request.headers.get("host", "")).hostname
    if hostname not in ("localhost", "127.0.0.1", "::1"):
        local = False
    if not local:
        raise ApiError(f"{scope}_LOCAL_ONLY", f"{label}仅允许从本机访问", status=403,
                       user_message=f"{label}仅限本机使用",
                       user_hint="请在运行本程序的电脑上打开这个页面")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get(header) != "1" or request.headers.get("sec-fetch-site") == "cross-site":
            raise ApiError(f"{scope}_ORIGIN", f"{label}请求缺少同源校验", status=403,
                           user_message=f"{label}请求无法验证来源",
                           user_hint="请刷新页面后重试")
        if origin and (urlsplit(origin).netloc != request.headers.get("host") or urlsplit(origin).scheme != request.url.scheme):
            raise ApiError(f"{scope}_ORIGIN", f"拒绝跨站{label}请求", status=403,
                           user_message=f"已拒绝来自其他页面的{label}请求",
                           user_hint="请在运行本程序的电脑上直接打开这个页面")


def manager(request):
    local_request(request)
    service = request.app.state.sync
    if request.method not in ("GET", "HEAD") and not service.started:
        raise ApiError("SYNC_NOT_STARTED", "数据目录尚未取得独占访问权", status=503,
                       user_message="同步服务还没有启动完成",
                       user_hint="请稍等片刻后重试；若一直如此，请确认程序没有被重复打开")
    return service


@router.get("/status")
def status(request: Request):
    return manager(request).status()


@router.get("/settings")
def settings(request: Request):
    return manager(request).settings


@router.patch("/settings")
def settings_update(request: Request, body: dict = Body(...)):
    return manager(request).settings_update(body)


@router.post("/test-connection")
def connection(request: Request):
    return manager(request).test_connection()


@router.post("/initialize")
def initialize(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise ApiError("CONFIRM_REQUIRED", "请确认初始化来源", status=409,
                       user_message="请先确认使用哪一侧的数据",
                       user_hint="确认后方可执行，避免覆盖现有数据")
    return manager(request).initialize(body.get("source"))


@router.post("/run")
def run(request: Request, body: dict = Body(default={})):
    return manager(request).run(synchronize=body.get("synchronize", False) is True)


@router.post("/offline")
def offline(request: Request):
    return manager(request).offline()


@router.get("/conflicts/{identity}")
def conflicts(request: Request, identity: str, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), unresolved: bool = False):
    return manager(request).conflict_page(identity, offset, limit, unresolved)


@router.patch("/conflicts/{identity}")
def resolve(request: Request, identity: str, body: dict = Body(...)):
    return manager(request).resolve(identity, body.get("revision"), body.get("choices", {}))


@router.post("/conflicts/{identity}/validate")
def validate(request: Request, identity: str, body: dict = Body(...)):
    return manager(request).validate_resolution(identity, body.get("revision"))


@router.post("/conflicts/{identity}/apply")
def apply(request: Request, identity: str, body: dict = Body(...)):
    return manager(request).apply_resolution(identity, body.get("revision"), body.get("confirm") is True)


@router.post("/conflicts/{identity}/cancel")
def cancel(request: Request, identity: str):
    return manager(request).cancel(identity)

@router.get("/conflicts/{identity}/details")
def details(request: Request, identity: str, conflict_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return manager(request).conflict_details(identity, conflict_id, offset, limit)


@router.get("/conflicts/{identity}/changes")
def changes(request: Request, identity: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return manager(request).changes_page(identity, offset, limit)


# ---------- 备份配置引导 ----------
# 环境检测与候选检查只读、不占用同步任务锁；安装、显式登录与最终应用在后端互斥执行。

@router.get("/onboarding/environment")
def onboarding_environment(request: Request):
    return manager(request).onboarding.environment()


@router.post("/onboarding/install")
def onboarding_install(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise ApiError("CONFIRM_REQUIRED", "请确认要安装的组件", status=409,
                       user_message="请先确认安装操作",
                       user_hint="安装会从官方来源下载并运行安装包，需要明确确认")
    return manager(request).onboarding.install(body.get("component"))


@router.get("/onboarding/github")
def onboarding_github(request: Request, account: str | None = Query(default=None)):
    return manager(request).onboarding.github_status(account)


@router.post("/onboarding/github/login")
def onboarding_github_login(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise ApiError("CONFIRM_REQUIRED", "请确认要进行的浏览器授权", status=409,
                       user_message="请先确认登录操作",
                       user_hint="登录会打开系统浏览器完成 GitHub 授权，需要明确确认")
    return manager(request).onboarding.github_login(body.get("account"), body.get("force") is True)


@router.post("/onboarding/check")
def onboarding_check(request: Request, body: dict = Body(...)):
    return manager(request).onboarding.check(body)


@router.post("/onboarding/complete")
def onboarding_complete(request: Request, body: dict = Body(...)):
    return manager(request).onboarding.complete(body)
