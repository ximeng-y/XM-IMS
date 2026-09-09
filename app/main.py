"""应用入口：create_app() 组装 /api/v1 路由、错误处理与数据目录初始化

启动：uvicorn app.main:app --port 8000
"""
from fastapi import FastAPI

from . import db
from .api.v1 import router as v1_router
from .errors import register_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(
        title="XM-IMS",
        description=(
            "个人电商进销存系统。\n\n"
            "约定：业务端点需 `X-Shop-Id` 请求头指定店铺（GET /api/v1/shops 查看列表）；"
            "错误统一为 `{code, message, hint?}`；写操作普遍支持 `dry_run` 预览，"
            "库存操作支持 `idempotency_key` 幂等。"
        ),
        version="0.1.0",
    )
    app.include_router(v1_router, prefix="/api/v1")
    register_error_handlers(app)
    db.init()
    return app


app = create_app()
