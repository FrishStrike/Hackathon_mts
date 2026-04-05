const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const findEl = (selector) => {
  if (!selector) return null;
  try {
    return document.querySelector(selector);
  } catch {
    return null;
  }
};

const waitForSelector = async (selector, timeoutMs = 10_000) => {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const el = findEl(selector);
    if (el) return el;
    if (Date.now() - start > timeoutMs) return null;
    await sleep(120);
  }
};

const scrollIntoViewCentered = (el) => {
  try {
    el.scrollIntoView({ behavior: "instant", block: "center", inline: "center" });
  } catch {
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
    } catch {}
  }
};

const click = async (selector) => {
  const el = findEl(selector);
  if (!el) throw new Error(`click: element not found: ${selector}`);
  scrollIntoViewCentered(el);
  el.click();
};

const typeText = async (selector, text, clear = true) => {
  const el = findEl(selector);
  if (!el) throw new Error(`type: element not found: ${selector}`);
  if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el.isContentEditable)) {
    throw new Error(`type: not an input/textarea/contenteditable: ${selector}`);
  }
  scrollIntoViewCentered(el);
  el.focus();

  if (clear) {
    if (el.isContentEditable) {
      el.textContent = "";
    } else {
      el.value = "";
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  if (el.isContentEditable) {
    el.textContent = String(text ?? "");
    el.dispatchEvent(new InputEvent("input", { bubbles: true, data: String(text ?? "") }));
  } else {
    el.value = String(text ?? "");
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
};

const scrollByDelta = async (deltaY) => {
  window.scrollBy({ top: Number(deltaY) || 0, left: 0, behavior: "smooth" });
  await sleep(250);
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg?.type !== "RUN_ACTION") {
        sendResponse({ ok: false, error: "Unknown message" });
        return;
      }

      const action = msg.action ?? {};
      const type = String(action.type ?? "");

      if (type === "waitFor") {
        const selector = String(action.selector ?? "");
        const timeoutMs = Number(action.timeoutMs ?? 10_000);
        const el = await waitForSelector(selector, timeoutMs);
        if (!el) throw new Error(`waitFor timeout: ${selector}`);
        sendResponse({ ok: true });
        return;
      }

      if (type === "click") {
        await click(String(action.selector ?? ""));
        sendResponse({ ok: true });
        return;
      }

      if (type === "type") {
        await typeText(String(action.selector ?? ""), String(action.text ?? ""), action.clear !== false);
        sendResponse({ ok: true });
        return;
      }

      if (type === "scroll") {
        await scrollByDelta(action.deltaY ?? 800);
        sendResponse({ ok: true });
        return;
      }

      if (type === "press") {
        const key = String(action.key ?? "");
        if (!key) throw new Error("press.key is required");
        document.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
        document.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
        sendResponse({ ok: true });
        return;
      }

      if (type === "hover") {
        const el = findEl(String(action.selector ?? ""));
        if (!el) throw new Error(`hover: element not found: ${action.selector}`);
        scrollIntoViewCentered(el);
        el.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
        el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        sendResponse({ ok: true });
        return;
      }

      if (type === "select") {
        const el = findEl(String(action.selector ?? ""));
        if (!el) throw new Error(`select: element not found: ${action.selector}`);
        if (el.tagName !== "SELECT") throw new Error(`select: not a <select>: ${action.selector}`);
        scrollIntoViewCentered(el);
        el.value = String(action.value ?? "");
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("input", { bubbles: true }));
        sendResponse({ ok: true });
        return;
      }

      if (type === "focus") {
        const el = findEl(String(action.selector ?? ""));
        if (!el) throw new Error(`focus: element not found: ${action.selector}`);
        scrollIntoViewCentered(el);
        el.focus();
        sendResponse({ ok: true });
        return;
      }

      if (type === "getAttribute") {
        const el = findEl(String(action.selector ?? ""));
        if (!el) throw new Error(`getAttribute: element not found: ${action.selector}`);
        const val = el.getAttribute(String(action.attr ?? ""));
        sendResponse({ ok: true, text: val ?? "" });
        return;
      }

      if (type === "getUrl") {
        sendResponse({ ok: true, text: window.location.href });
        return;
      }

      if (type === "extractText") {
        const el = findEl(String(action.selector ?? ""));
        if (!el) throw new Error(`extractText: element not found: ${action.selector}`);
        sendResponse({ ok: true, text: el.textContent ?? "" });
        return;
      }

      if (type === "extractInteractive") {
        const elements = [];
        const seen = new Set();
        const MAX_ELEMENTS = 40;

        const buildSelector = (el) => {
          if (el.id) return `#${CSS.escape(el.id)}`;
          if (el.dataset?.testid) return `[data-testid="${el.dataset.testid}"]`;
          if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
          if (el.getAttribute("aria-label")) return `${el.tagName.toLowerCase()}[aria-label="${el.getAttribute("aria-label")}"]`;
          const tag = el.tagName.toLowerCase();
          const cls = Array.from(el.classList).slice(0, 2).join(".");
          if (cls) return `${tag}.${cls}`;
          return tag;
        };

        const selectors = [
          "a[href]", "button", "input:not([type=hidden])",
          "textarea", "select", "[role=button]", "[role=link]", "[onclick]",
        ];

        for (const sel of selectors) {
          if (elements.length >= MAX_ELEMENTS) break;
          for (const el of document.querySelectorAll(sel)) {
            if (elements.length >= MAX_ELEMENTS) break;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            if (el.offsetParent === null && el.tagName !== "BODY") continue;

            const text = (el.textContent || el.value || el.placeholder || el.alt || "").trim().slice(0, 60);
            if (!text) continue;

            const selector = buildSelector(el);
            const key = `${selector}|${text}`;
            if (seen.has(key)) continue;
            seen.add(key);

            const tag = el.tagName.toLowerCase();
            const href = el.href ? ` href="${el.href.slice(0, 80)}"` : "";
            const type_attr = el.type ? ` type="${el.type}"` : "";
            elements.push(`[${tag}${type_attr}${href}] selector="${selector}" text="${text}"`);
          }
        }

        sendResponse({ ok: true, text: elements.join("\n") });
        return;
      }

      sendResponse({ ok: false, error: `Unsupported action: ${type}` });
    } catch (e) {
      sendResponse({ ok: false, error: e.message });
    }
  })();

  return true;
});

