"""前端独立入口（供双端口启动）：页面 + 静态资源，/api/v1 反向代理到后端

设计：浏览器只访问前端端口（同源），所有 /api/v1 JSON 请求由本应用的
代理转发到后端进程（地址取环境变量 XM_API_BASE，由 launcher.py 注入
后端实际端口，端口顺延后前后端仍能对上）。同源意味着无需 CORS，
Cookie 与自定义请求头天然可用，前端 JS / 模板无需任何改动。
"""
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import web

# 后端地址：launcher.py 注入实际端口；未注入时回落默认端口
API_BASE = os.environ.get("XM_API_BASE", "http://127.0.0.1:33111")

# 只转发业务相关请求头，跳过后端会自行生成的 hop-by-hop 头（Host/Content-Length/编码等）
_FORWARD_HEADERS = (
    "content-type", "content-disposition", "accept",
    "x-shop-id", "x-api-token", "authorization", "x-idempotency-key",
)


def _forward(method: str, path: str, query: str, body: bytes, headers: dict) -> Response:
    url = f"{API_BASE}/api/v1/{path}"
    if query:
        url += "?" + query
    req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            headers = {}
            if resp.headers.get("content-type"):
                headers["content-type"] = resp.headers["content-type"]
            return Response(resp.read(), status_code=resp.status, headers=headers)
    except urllib.error.HTTPError as e:
        # 后端返回的业务/校验错误（含结构化 {code,message,hint}）原样透传
        resp_headers = {}
        if e.headers and e.headers.get("content-type"):
            resp_headers["content-type"] = e.headers["content-type"]
        return Response(e.read(), status_code=e.code, headers=resp_headers)
    except urllib.error.URLError:
        return JSONResponse(
            status_code=502,
            content={
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "后端服务不可用，请稍后重试或关闭窗口重新启动",
            },
        )


proxy = APIRouter()


@proxy.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def api_proxy(path: str, request: Request):
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_HEADERS}
    body = await request.body()
    return await run_in_threadpool(
        _forward, request.method, path, request.url.query, body, headers)


def create_web_app() -> FastAPI:
    app = FastAPI(
        title="XM-IMS Frontend",
        version="0.1.0",
        docs_url=None, redoc_url=None, openapi_url=None,  # 文档属于后端进程
    )
    app.include_router(web.router)
    app.include_router(proxy)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")
    return app


app = create_web_app()
