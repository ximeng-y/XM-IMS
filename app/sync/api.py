"""仅本机可访问的同步管理接口。"""
import ipaddress
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, Body, Query
from ..errors import ApiError

router = APIRouter(prefix="/sync", tags=["数据同步"])


def local_request(request):
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    hostname = urlsplit("http://" + request.headers.get("host", "")).hostname
    if hostname not in ("localhost", "127.0.0.1", "::1"):
        local = False
    if not local:
        raise ApiError("SYNC_LOCAL_ONLY", "同步管理仅允许从本机访问", status=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("x-xm-sync") != "1" or request.headers.get("sec-fetch-site") == "cross-site":
            raise ApiError("SYNC_ORIGIN", "同步请求缺少同源校验", status=403)
        if origin and (urlsplit(origin).netloc != request.headers.get("host") or urlsplit(origin).scheme != request.url.scheme):
            raise ApiError("SYNC_ORIGIN", "拒绝跨站同步请求", status=403)


def manager(request):
    local_request(request)
    service = request.app.state.sync
    if request.method not in ("GET", "HEAD") and not service.started:
        raise ApiError("SYNC_NOT_STARTED", "数据目录尚未取得独占访问权", status=503)
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
        raise ApiError("CONFIRM_REQUIRED", "请确认初始化来源", status=409)
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
