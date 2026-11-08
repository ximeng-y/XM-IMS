"""前端页面骨架与模块资源装配。

页面交互由浏览器原生 ES modules 直接调用 /api/v1 JSON 端点完成，这里只负责：
- 渲染 HTML 骨架（不进 OpenAPI 契约）
- 逐项声明模块名、静态 URL 与文件版本，供模板输出 import map
- 声明每个页面的入口模块与所需样式
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# 稳定模块名 → (静态 URL, 文件版本)。逐项声明，不扫描目录自动注册。
# 页面与组件只用稳定名互相导入，版本号集中在这里；任何文件内容有实质改动都必须更新它自己的版本，
# 只改启动入口的版本不足以让浏览器更新已缓存的子模块。
MODULES = {
    "xm/api": ("/static/js/api.js", 4),
    "xm/ui": ("/static/js/ui.js", 20),
    "xm/shop-context": ("/static/js/runtime/shop-context.js", 1),
    "xm/sync": ("/static/js/sync-common.js", 7),
    "xm/update": ("/static/js/update.js", 5),
    "xm/responsive": ("/static/js/runtime/responsive.js", 1),
    "xm/shell": ("/static/js/runtime/shell.js", 1),
    "xm/components/context-menu": ("/static/js/components/context-menu.js", 3),
    "xm/components/local-backups": ("/static/js/components/local-backups.js", 2),
    "xm/components/delete-flow": ("/static/js/components/delete-flow.js", 2),
    "xm/components/formula-editor": ("/static/js/components/formula-editor.js", 4),
    "xm/components/item-form": ("/static/js/components/item-form.js", 9),
    "xm/components/item-picker": ("/static/js/components/item-picker.js", 1),
    "xm/components/ops-forms": ("/static/js/components/ops-forms.js", 9),
    "xm/pages/overview": ("/static/js/pages/overview.js", 7),
    "xm/pages/inventory": ("/static/js/pages/inventory.js", 18),
    "xm/pages/inventory-data": ("/static/js/pages/inventory-data.js", 1),
    "xm/pages/inventory-viewport": ("/static/js/pages/inventory-viewport.js", 1),
    "xm/pages/items": ("/static/js/pages/items.js", 14),
    "xm/pages/spus": ("/static/js/pages/spus.js", 7),
    "xm/pages/fields": ("/static/js/pages/fields.js", 11),
    "xm/pages/movements": ("/static/js/pages/movements.js", 4),
    "xm/pages/shops": ("/static/js/pages/shops.js", 4),
    "xm/pages/sync": ("/static/js/pages/sync.js", 13),
    "xm/pages/settings": ("/static/js/pages/settings.js", 2),
    "xm/onboarding/wizard": ("/static/onboarding/wizard.js", 2),
    "xm/onboarding/backup": ("/static/onboarding/backup.js", 3),
    "xm/onboarding/first-use": ("/static/onboarding/first-use.js", 3),
}

# 样式资源同样逐项登记；页面清单只引用这里的键。
# 加载顺序：基础样式（base.html 固定引用）→ 组件样式 → 页面样式。
STYLES = {
    "css/components/formula-editor": ("/static/css/components/formula-editor.css", 1),
    "css/components/item-form": ("/static/css/components/item-form.css", 1),
    "css/pages/catalog": ("/static/css/pages/catalog.css", 1),
    "css/pages/inventory": ("/static/css/pages/inventory.css", 1),
    "css/pages/movements": ("/static/css/pages/movements.css", 1),
    "css/pages/overview": ("/static/css/pages/overview.css", 1),
    "css/pages/shops": ("/static/css/pages/shops.css", 2),
    "css/pages/sync": ("/static/css/pages/sync.css", 2),
    "css/onboarding": ("/static/onboarding/onboarding.css", 4),
}

# 启动入口：所有页面共用同一模块，页面能力由入口模块导出的 page 描述决定。
BOOTSTRAP = ("/static/js/runtime/bootstrap.js", 2)

# 页面清单：入口模块 + 该页需要的样式；页面名称与 body[data-page] 一致（现有 CSS 仍使用它）。
# 组件样式排在页面样式之前，页面样式可覆盖组件的同名规则。
PAGES = {
    "overview": {"module": "xm/pages/overview", "styles": ("css/pages/overview",)},
    "inventory": {"module": "xm/pages/inventory",
                  "styles": ("css/components/item-form", "css/pages/inventory")},
    "items": {"module": "xm/pages/items",
              "styles": ("css/components/item-form", "css/pages/catalog")},
    "spus": {"module": "xm/pages/spus", "styles": ("css/pages/catalog",)},
    "fields": {"module": "xm/pages/fields",
               "styles": ("css/components/formula-editor", "css/pages/catalog")},
    "movements": {"module": "xm/pages/movements", "styles": ("css/pages/movements",)},
    "shops": {"module": "xm/pages/shops", "styles": ("css/pages/shops",)},
    "sync": {"module": "xm/pages/sync", "styles": ("css/pages/sync", "css/onboarding")},
    "settings": {"module": "xm/pages/settings", "styles": ()},
    "onboarding": {"module": "xm/onboarding/first-use", "styles": ("css/onboarding",)},
}


def _asset_url(url: str, version: int) -> str:
    return f"{url}?v={version}"


def _page(template: str, key: str):
    spec = PAGES[key]
    bootstrap = {"url": _asset_url(*BOOTSTRAP)}
    import_map = {
        "imports": {name: _asset_url(url, version) for name, (url, version) in MODULES.items()}
    }
    styles = [{"url": _asset_url(*STYLES[name])} for name in spec["styles"]]

    async def render(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, template, {
            "import_map": import_map,
            "page_entry": spec["module"],
            "page_styles": styles,
            "bootstrap": bootstrap,
        })
    return render


router.get("/", response_class=HTMLResponse)(_page("overview.html", "overview"))
router.get("/inventory", response_class=HTMLResponse)(_page("inventory.html", "inventory"))
router.get("/items", response_class=HTMLResponse)(_page("items.html", "items"))
router.get("/spus", response_class=HTMLResponse)(_page("spus.html", "spus"))
router.get("/fields", response_class=HTMLResponse)(_page("fields.html", "fields"))
router.get("/movements", response_class=HTMLResponse)(_page("movements.html", "movements"))
router.get("/shops", response_class=HTMLResponse)(_page("shops.html", "shops"))

router.get("/sync", response_class=HTMLResponse)(_page("sync.html", "sync"))
router.get("/settings", response_class=HTMLResponse)(_page("settings.html", "settings"))
router.get("/onboarding", response_class=HTMLResponse)(
    _page("onboarding/first-use.html", "onboarding"))
