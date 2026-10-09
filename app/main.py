"""应用入口：create_app() 组装 /api/v1 路由、验证前端页面、错误处理与数据目录初始化

启动：uvicorn app.main:app --port 8000
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, web
from .api.v1 import router as v1_router
from .errors import register_error_handlers
from .version import APP_VERSION
from .sync.runtime import lifespan, install


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="XM-IMS",
        description=(
            "个人电商进销存系统。\n\n"
            "约定：业务端点需 `X-Shop-Id` 请求头指定店铺（GET /api/v1/shops 查看列表）；"
            "错误统一为 `{code, message, hint?}`；写操作普遍支持 `dry_run` 预览，"
            "库存操作支持 `idempotency_key` 幂等。"
        ),
        version=APP_VERSION,
    )
    app.include_router(v1_router, prefix="/api/v1")
    # 验证前端：页面路由（web.py）+ 静态资源
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")
    register_error_handlers(app)
    install(app)
    return app


app = create_app()
