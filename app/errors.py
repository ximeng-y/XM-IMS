"""统一结构化错误：{"code", "message", "hint"?}

所有业务错误都通过 ApiError 抛出，由全局异常处理器归一化为该格式；
FastAPI 的 422 校验错误也归一化为同格式（code=VALIDATION_ERROR）。
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, status: int = 400):
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        payload = {"code": exc.code, "message": exc.message}
        if exc.hint:
            payload["hint"] = exc.hint
        return JSONResponse(status_code=exc.status, content=payload)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # hint 汇总各字段的具体校验错误，便于 Agent 自行修复请求
        detail = "；".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "hint": detail,
            },
        )
