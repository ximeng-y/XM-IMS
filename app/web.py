"""验证前端页面路由：Jinja2 渲染页面骨架（不进 OpenAPI 契约）

本前端的目的是验证后端 /api/v1 JSON 契约本身，因此页面交互由前端 JS
直接调用真实 API 端点（携带 X-Shop-Id、体验 dry_run / 幂等键 / 结构化
错误），此处仅提供 HTML 骨架与静态资源。
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _page(template: str):
    async def render(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, template)
    return render


router.get("/", response_class=HTMLResponse)(_page("overview.html"))
router.get("/inventory", response_class=HTMLResponse)(_page("inventory.html"))
router.get("/items", response_class=HTMLResponse)(_page("items.html"))
router.get("/spus", response_class=HTMLResponse)(_page("spus.html"))
router.get("/fields", response_class=HTMLResponse)(_page("fields.html"))
router.get("/movements", response_class=HTMLResponse)(_page("movements.html"))
