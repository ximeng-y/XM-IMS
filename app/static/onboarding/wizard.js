/* 轻量步骤容器：业务检测只在进入步骤时执行，输入变化只刷新导航。 */
"use strict";

class OnboardingWizard {
  constructor({ steps, context, container, onComplete, onCancel }) {
    this.steps = steps;
    this.context = context;
    this.container = container;
    this.onComplete = onComplete;
    this.onCancel = onCancel;
    this.index = 0;
    this.busy = false;
    this.entered = false;
    const make = (tag, cls) => {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      return node;
    };
    this.heading = make("h2", "sync-onboarding-counter");
    this.content = make("div", "sync-onboarding-steps");
    this.error = make("div", "sync-onboarding-error");
    this.error.hidden = true;
    this.error.setAttribute("role", "alert");
    this.notice = make("p", "sync-onboarding-note");
    this.notice.setAttribute("aria-live", "polite");
    const foot = make("div", "onboarding-footer");
    const button = (title, cls) => {
      const node = make("button", cls);
      node.type = "button";
      node.textContent = title;
      return node;
    };
    this.cancel = button("取消", "btn-outline");
    this.back = button("上一步", "btn-outline");
    this.next = button("下一步");
    foot.append(this.cancel, this.back, this.next);
    container.replaceChildren(this.heading, this.content, this.error, this.notice, foot);
    this.cancel.onclick = () => this.run(() => this.onCancel?.(this.context));
    this.back.onclick = () => this.run(async () => { this.index--; await this.enter(); });
    this.next.onclick = () => this.run(async () => {
      const step = this.current();
      if (step.canNext?.(this.context) || !this.entered) return;
      if (step.onPrimary) await step.onPrimary(this.context, this);
      else if (this.index < this.visible().length - 1) {
        this.index++;
        await this.enter();
      } else await this.finish();
    });
  }
  visible() { return this.steps.filter(s => !s.when || s.when(this.context)); }
  current() {
    const list = this.visible();
    this.index = Math.max(0, Math.min(this.index, list.length - 1));
    return list[this.index];
  }
  updateNav() {
    const step = this.current();
    const reason = step.canNext?.(this.context);
    this.next.disabled = this.busy || !this.entered || !!reason;
    this.back.disabled = this.busy || this.index === 0 || this.context.lockBack === true;
    this.cancel.disabled = this.busy;
    this.cancel.hidden = !this.onCancel;
    this.next.textContent = step.primaryLabel?.(this.context) || "下一步";
    this.notice.textContent = this.busy ? "正在处理，请稍候…" : (reason || "");
  }
  render() {
    const step = this.current();
    this.heading.textContent = step.title;
    this.content.replaceChildren();
    step.render(this.context, this.content, this);
    this.updateNav();
  }
  showError(error) {
    this.error.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = error?.user_message || "操作暂未完成，请重试";
    this.error.append(title);
    if (error?.user_hint) {
      const hint = document.createElement("p");
      hint.textContent = error.user_hint;
      this.error.append(hint);
    }
    this.error.hidden = false;
  }
  async enter() {
    const perform = async () => {
      this.entered = false;
      this.error.hidden = true;
      this.heading.textContent = this.current().title;
      this.content.replaceChildren();
      this.updateNav();
      try {
        await this.current().enter?.(this.context, this);
        this.entered = true;
        this.render();
      } catch (error) {
        this.showError(error);
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn-outline";
        retry.textContent = "重试";
        retry.onclick = () => this.run(() => this.enter());
        this.content.append(retry);
      }
    };
    if (this.busy) await perform();
    else await this.run(perform);
  }
  async go(id) {
    const index = this.visible().findIndex(s => s.id === id);
    if (index < 0) return;
    this.index = index;
    await this.enter();
  }
  async run(action) {
    if (this.busy) return;
    this.busy = true;
    this.error.hidden = true;
    this.container.querySelectorAll("input,select,button").forEach(el => {
      el.dataset.onboardingDisabled = String(el.disabled);
      el.disabled = true;
    });
    this.updateNav();
    try { await action(); }
    catch (error) { this.showError(error); }
    finally {
      this.busy = false;
      this.container.querySelectorAll("[data-onboarding-disabled]").forEach(el => {
        el.disabled = el.dataset.onboardingDisabled === "true";
        delete el.dataset.onboardingDisabled;
      });
      this.updateNav();
    }
  }
  async finish() { await this.onComplete?.(this.context); }
}

export { OnboardingWizard };
