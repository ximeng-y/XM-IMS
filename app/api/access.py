"""进程边界共享的本机/同源访问校验。

同步管理与程序更新都只允许从本机页面发起；前端代理在转发前用同一套规则
校验真实客户端。本模块只做校验与从 app.state 取服务，不导入业务路由。
"""
import ipaddress
from urllib.parse import urlsplit

from ..errors import ApiError


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


def update_request(request):
    """在直连后端和前端代理边界使用同一访问规则。"""
    local_request(request, scope="UPDATE", header="x-xm-update", label="程序更新")


def require_sync_manager(request):
    """同步管理接口的统一前置：本机校验 + 写操作需要服务已取得独占访问权。"""
    local_request(request)
    service = request.app.state.sync
    if request.method not in ("GET", "HEAD") and not service.started:
        raise ApiError("SYNC_NOT_STARTED", "数据目录尚未取得独占访问权", status=503,
                       user_message="同步服务还没有启动完成",
                       user_hint="请稍等片刻后重试；若一直如此，请确认程序没有被重复打开")
    return service
