/* 通用删除流程：危险确认框 → DELETE …?confirm=true → 成功提示 → 刷新列表

   - 只负责这条固定链路，全部面向用户的文案由调用方传入，组件不做业务判定
   - precheck 供调用方做前置拦截（如 SPU 下还有单品时直接提示，不发请求）
   - 单例导出：与 ContextMenu / ItemForm / OpsForm 同样挂全局常量，随 base.html 加载 */
"use strict";

const DeleteFlow = (() => {
  // 调用方未给 okText 时的兜底：只报出被删对象的名称
  const defaultOkText = (res) => String(res && res.name != null ? res.name : "");

  // opts:
  //   title       确认框标题
  //   bodyHtml    确认框正文 HTML（调用方自行转义）
  //   confirmText 确认按钮文字，默认「确认删除」
  //   precheck    可选，( ) => false 表示前置条件不满足，已自行提示并中止
  //   path        DELETE 路径（须自带 ?confirm=true）
  //   okText      可选，(res) => string；返回空串则不提示
  //   onDone      可选，成功后的刷新回调
  //   failPrefix  失败提示前缀，默认「删除失败」
  async function run({ title, bodyHtml, confirmText = "确认删除", precheck, path,
                       okText = defaultOkText, onDone, failPrefix = "删除失败" }) {
    if (precheck && precheck() === false) return false;
    const confirmed = await UI.confirmDialog({ title, bodyHtml, confirmText, danger: true });
    if (!confirmed) return false;
    try {
      const res = await XM.del(path);
      const text = okText(res);
      if (text) UI.ok(text);
      if (onDone) onDone(res);
      return true;
    } catch (e) {
      UI.apiFail(e, failPrefix);
      return false;
    }
  }

  return { run };
})();
