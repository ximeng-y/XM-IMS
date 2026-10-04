"""后端独立入口（供双端口启动）：仅 /api/v1 JSON 契约，不含页面

与 app.main 的区别：不挂载页面路由与静态资源，使前端反向代理时
后端只暴露 API 契约；前端页面由 app.web_app 单独提供。
"""
from fastapi import FastAPI

from . import db
from .api.v1 import router as v1_router
from .errors import register_error_handlers
from .sync.runtime import lifespan, install


def create_api_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="XM-IMS API",
        description=(
            "后端 JSON API 契约。约定：业务端点需 `X-Shop-Id` 请求头指定店铺"
            "（GET /api/v1/shops 查看列表）；错误统一为 `{code, message, hint?}`；"
            "写操作普遍支持 `dry_run` 预览，库存操作支持 `idempotency_key` 幂等。"
        ),
        version="0.1.0",
    )
    app.include_router(v1_router, prefix="/api/v1")
    register_error_handlers(app)
    install(app)
    return app


app = create_api_app()
