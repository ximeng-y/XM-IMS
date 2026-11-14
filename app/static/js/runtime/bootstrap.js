/* 页面启动入口：所有页面共用，按页面描述的能力决定安装顺序。

   顺序固定为：外壳（导航、表格提示、右键基础监听、顶栏）→ 自动更新提示
   → 同步就绪检查 → 店铺上下文 → 页面 init。任一步失败或发生跳转即停止，
   不再继续后面的业务初始化。 */
"use strict";

import { UI } from "xm/ui";
import { Sync } from "xm/sync";
import { initResponsive } from "xm/responsive";
import { installShell, loadShops } from "xm/shell";
import { startAutoCheck } from "xm/update";
import { Desktop } from "xm/runtime/desktop";

// 页面描述缺省值：新增页面必须显式声明能力，未声明时不获得业务上下文。
const DEFAULT_PAGE = {
  shell: false, shopContext: "none", requireSyncReady: false, autoUpdatePrompt: false,
  init: async () => {},
};

function highlightNav() {
  const page = document.body.dataset.page;
  UI.$$(".sidenav a").forEach((a) => {
    if (a.dataset.nav === page) {
      a.classList.add("active");
      a.setAttribute("aria-current", "page");
    }
  });
}

let booted = false;

export async function boot(module) {
  if (booted) return;
  booted = true;
  const spec = { ...DEFAULT_PAGE, ...(module.page || {}) };

  if (!spec.shell) {
    // 独立页面不加载普通外壳：不触碰 shopSelect/btnNewShop/toasts，也不拦截全站右键
    Sync.initSync();
    await spec.init({});
    confirmDesktopReady();
    return;
  }

  // 导航与提示先就位：同步检查期间仍要能切换页面
  initResponsive();
  installShell();
  highlightNav();
  if (spec.autoUpdatePrompt) startAutoCheck();

  // 非本机降级必须在分支之前无条件执行：ensureReady 在非本机时直接返回 true，
  // 若只在 else 分支调用，局域网访问普通业务页时同步入口不会被隐藏
  Sync.initSync();

  // 同步检查失败或发生跳转时不再继续
  if (spec.requireSyncReady) {
    try { if (!await Sync.ensureReady()) return; }
    catch (e) { UI.fail("无法检查同步状态：" + e.message); return; }
  }

  if (spec.shopContext === "none") {
    const box = UI.$(".shop-box");
    if (box) box.hidden = true;
  } else {
    // required 与 optional 都载入店铺；无店铺时的提示与按钮禁用已在 shell 内应用，
    // optional（店铺管理页）不参与整页禁用，页面自行决定是否继续
    const loaded = await loadShops({ isShopsPage: spec.shopContext === "optional" });
    if (!loaded) return;
  }

  try {
    await spec.init({ shopContext: spec.shopContext });
    // 首屏确认只在本页面初始化成功后上报；失败时保持候选未就绪，由监督回退
    confirmDesktopReady();
  } catch (e) {
    console.error(e);
    UI.fail("页面初始化失败：" + (e.message || e));
  }
}

/** 首屏本地模块初始化成功后通知桌面；不等待远端更新检查。
    桌面首次启动用它确认候选版本可用；普通启动与源码模式没有影响。 */
function confirmDesktopReady() {
  if (!Desktop.available()) return;
  Desktop.confirmPageReady().catch(() => {});
}
