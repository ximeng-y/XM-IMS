"""统一结构化错误：{"code", "message", "hint"?, "user_message"?, "user_hint"?}

两套文案各司其职（Agent 与界面共用同一套接口，但受众不同）：
    message / hint          —— 面向 API 调用方（Agent）：可含端点、请求头、参数名、枚举值等
                               自修复线索；不保证适合直接展示给普通用户
    user_message/user_hint  —— 面向页面用户：纯中文、不出现端点/状态码/参数名；缺失时界面
                               回落 message，且不展示 hint，避免接口类文案泄漏到界面

所有业务错误都通过 ApiError 抛出，由全局异常处理器归一化为该格式；
FastAPI 的 422 校验错误也归一化为同格式（code=VALIDATION_ERROR）。
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, status: int = 400,
                 user_message: str | None = None, user_hint: str | None = None):
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status
        self.user_message = user_message
        self.user_hint = user_hint


def error_payload(exc: ApiError) -> dict:
    """ApiError → 响应体；面向用户的字段仅在提供时出现"""
    payload = {"code": exc.code, "message": exc.message}
    if exc.hint:
        payload["hint"] = exc.hint
    if exc.user_message:
        payload["user_message"] = exc.user_message
    if exc.user_hint:
        payload["user_hint"] = exc.user_hint
    return payload


def error_dict(code: str, message: str, hint: str | None = None,
               user_message: str | None = None, user_hint: str | None = None) -> dict:
    """非 ApiError 场合（中间件、后台线程）构造同样双轨的响应体/状态条目"""
    return error_payload(ApiError(code, message, hint=hint, user_message=user_message,
                                  user_hint=user_hint))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=error_payload(exc))

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
                "user_message": "提交的内容不符合要求",
                "user_hint": "请检查各字段是否已按提示填写",
            },
        )
