const DEFAULT_PLANNER_URL = "http://localhost:8001/api/plan";
const DEFAULT_STEP_URL = "http://localhost:8001/api/step";
const ML_SERVICE_URL = "http://localhost:8001";

let isRunning = false;
let abortRequested = false;

// ─── Слушаем pendingBrowserAction из chrome.storage ───────────
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.pendingBrowserAction) return;
  const { prompt, queryId } = changes.pendingBrowserAction.newValue ?? {};
  if (!prompt || !queryId) return;

  // Сбрасываем предыдущий зависший запуск
  isRunning = false;
  abortRequested = false;

  executeBrowserAction(prompt, queryId).catch((e) => {
    console.error("[BG] executeBrowserAction error:", e.message);
  });

  // Очищаем чтобы повторно не сработало
  chrome.storage.local.remove("pendingBrowserAction");
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const sendToPanel = async (msg) => {
  try {
    await chrome.runtime.sendMessage(msg);
  } catch {
    // side panel might be closed
  }
};

const log = (line) => sendToPanel({ type: "LOG", line });
const setStatus = (kind, text) => sendToPanel({ type: "STATUS", kind, text });

const waitForTabComplete = async (tabId, timeoutMs = 30_000) => {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      if (Date.now() - start > timeoutMs) {
        clearInterval(timer);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        reject(new Error("Timeout ожидания загрузки страницы"));
      }
    }, 250);

    const onUpdated = (id, info) => {
      if (id !== tabId) return;
      if (info.status === "complete") {
        clearInterval(timer);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
  });
};

const runActionInTab = async (tabId, action) => {
  const resp = await chrome.tabs.sendMessage(tabId, { type: "RUN_ACTION", action });
  if (!resp?.ok) throw new Error(resp?.error ?? "Action failed");
  return resp;
};

const getPageText = async (tabId) => {
  try {
    const resp = await chrome.tabs.sendMessage(tabId, {
      type: "RUN_ACTION",
      action: { type: "extractText", selector: "body" }
    });
    return resp?.text?.slice(0, 3000) ?? "";
  } catch {
    return "";
  }
};

const getInteractiveElements = async (tabId) => {
  try {
    const resp = await chrome.tabs.sendMessage(tabId, {
      type: "RUN_ACTION",
      action: { type: "extractInteractive" }
    });
    return resp?.text ?? "";
  } catch {
    return "";
  }
};

const ensureContentScript = async (tabId) => {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["contentScript.js"],
    });
  } catch {
    // already injected or no access
  }
  await sleep(100);
};

// ─── Управление вкладками ───────────────────────────────────
// Хранилище открытых агентом вкладок: { alias -> tabId }
let agentTabs = {};

const openNewTab = async (url, alias) => {
  const tab = await chrome.tabs.create({ url: url || "about:blank", active: true });
  const key = alias || `tab_${tab.id}`;
  agentTabs[key] = tab.id;
  await log(`NEW TAB [${key}]: ${url || "about:blank"}`);
  if (url && url !== "about:blank") {
    try { await waitForTabComplete(tab.id, 15000); } catch { await log("WARN: page load timeout"); }
    await sleep(500);
    await ensureContentScript(tab.id);
  }
  return tab.id;
};

const switchToTab = async (alias) => {
  const tabId = agentTabs[alias];
  if (!tabId) throw new Error(`switchTab: unknown alias "${alias}"`);
  await chrome.tabs.update(tabId, { active: true });
  await log(`SWITCH TAB: ${alias}`);
  return tabId;
};

const closeTab = async (alias) => {
  const tabId = agentTabs[alias];
  if (!tabId) throw new Error(`closeTab: unknown alias "${alias}"`);
  await chrome.tabs.remove(tabId);
  delete agentTabs[alias];
  await log(`CLOSE TAB: ${alias}`);
};

const listAgentTabs = async () => {
  const info = {};
  for (const [alias, tabId] of Object.entries(agentTabs)) {
    try {
      const tab = await chrome.tabs.get(tabId);
      info[alias] = { id: tabId, url: tab.url, title: tab.title };
    } catch {
      delete agentTabs[alias];
    }
  }
  return info;
};

// ─── Пошаговый агент (через /api/step + Gemini) ──────────────
const executeAgentLoop = async (prompt) => {
  isRunning = true;
  abortRequested = false;
  agentTabs = {};
  await setStatus("busy", "thinking…");

  const history = [];
  const MAX_STEPS = 10;

  try {
    // Создаём ОТДЕЛЬНУЮ вкладку для работы агента
    const tabId = await openNewTab("about:blank", "main");

    for (let i = 0; i < MAX_STEPS; i++) {
      if (abortRequested) throw new Error("Stopped by user");

      // Получаем актуальное состояние нашей вкладки
      const currentTab = await chrome.tabs.get(tabId);
      const currentUrl = currentTab.url || "";

      // Инжектим content script после навигации
      if (currentUrl.startsWith("http")) {
        await ensureContentScript(tabId);
      }

      const pageText = currentUrl.startsWith("http") ? await getPageText(tabId) : "";
      const interactive = currentUrl.startsWith("http") ? await getInteractiveElements(tabId) : "";

      // Собираем инфо об открытых вкладках
      const tabsInfo = await listAgentTabs();

      await log(`STEP ${i + 1}: url=${currentUrl.slice(0, 60)}...`);

      const resp = await fetch(DEFAULT_STEP_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          url: currentUrl,
          page_text: pageText,
          interactive_elements: interactive,
          history,
          open_tabs: tabsInfo,
        }),
      });

      const action = await resp.json();
      await log(`ACTION: ${JSON.stringify(action)}`);

      if (action.type === "done") {
        await setStatus("ok", "done");
        await log(`ANSWER: ${action.answer}`);
        await sendToPanel({ type: "ANSWER", text: action.answer });
        break;
      }

      history.push(action);

      // ── Действия с вкладками (обрабатываются в background) ──
      if (action.type === "newTab") {
        await openNewTab(action.url || "about:blank", action.alias);
        await sleep(300);
        continue;
      }

      if (action.type === "switchTab") {
        await switchToTab(action.alias);
        await sleep(300);
        continue;
      }

      if (action.type === "closeTab") {
        await closeTab(action.alias);
        await sleep(200);
        continue;
      }

      if (action.type === "navigate") {
        // Навигация в текущей (или указанной) вкладке
        const targetTabId = action.alias ? (agentTabs[action.alias] || tabId) : tabId;
        await chrome.tabs.update(targetTabId, { url: action.url });
        try {
          await waitForTabComplete(targetTabId, 15000);
        } catch {
          await log("WARN: page load timeout, continuing...");
        }
        await sleep(1000);
        if (action.url.startsWith("http")) {
          await ensureContentScript(targetTabId);
        }
        continue;
      }

      // click/type/scroll/hover/select и пр. — через content script
      if (currentUrl.startsWith("http")) {
        await ensureContentScript(tabId);
        try {
          await runActionInTab(tabId, action);
        } catch (e) {
          await log(`WARN: action failed: ${e.message}`);
        }
      }
      await sleep(300);
    }

    await setStatus("ok", "done");
    await log("DONE");
  } catch (e) {
    await setStatus("err", "error");
    await log(`ERROR: ${e.message}`);
  } finally {
    isRunning = false;
    abortRequested = false;
  }
};

// ─── Выполнение браузерной команды в реальном браузере ────────
const sendTrace = async (queryId, step, status, detail = "") => {
  try {
    await fetch(`${ML_SERVICE_URL}/internal/trace`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query_id: queryId, event: { step, status, detail } }),
    });
  } catch {}
};

const executeBrowserAction = async (prompt, queryId) => {
  isRunning = true;
  abortRequested = false;
  agentTabs = {};

  const history = [];
  const MAX_STEPS = 10;
  const sources = [];

  try {
    await sendTrace(queryId, "search", "processing", "Юми открывает браузер...");

    // Создаём реальную вкладку
    const tabId = await openNewTab("about:blank", "main");

    for (let i = 0; i < MAX_STEPS; i++) {
      if (abortRequested) throw new Error("Stopped by user");

      const currentTab = await chrome.tabs.get(tabId);
      const currentUrl = currentTab.url || "";

      if (currentUrl.startsWith("http")) {
        await ensureContentScript(tabId);
      }

      const pageText = currentUrl.startsWith("http") ? await getPageText(tabId) : "";
      const interactive = currentUrl.startsWith("http") ? await getInteractiveElements(tabId) : "";
      const tabsInfo = await listAgentTabs();

      await sendTrace(queryId, "browser", "processing", `Шаг ${i + 1}: ${currentUrl.slice(0, 60)}`);
      await log(`STEP ${i + 1}: url=${currentUrl.slice(0, 60)}...`);

      const resp = await fetch(DEFAULT_STEP_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          url: currentUrl,
          page_text: pageText,
          interactive_elements: interactive,
          history,
          open_tabs: tabsInfo,
        }),
      });

      const action = await resp.json();
      await log(`ACTION: ${JSON.stringify(action)}`);

      if (action.type === "done") {
        // Отправляем результат на бэкенд
        await fetch(`${ML_SERVICE_URL}/api/browser-done/${queryId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer: action.answer, sources }),
        });
        await log(`ANSWER: ${action.answer}`);
        await sendToPanel({ type: "ANSWER", text: action.answer });
        break;
      }

      history.push(action);

      // Вкладки
      if (action.type === "newTab") {
        await openNewTab(action.url || "about:blank", action.alias);
        if (action.url) sources.push(action.url);
        await sleep(300);
        continue;
      }
      if (action.type === "switchTab") {
        await switchToTab(action.alias);
        await sleep(300);
        continue;
      }
      if (action.type === "closeTab") {
        await closeTab(action.alias);
        await sleep(200);
        continue;
      }

      // Навигация
      if (action.type === "navigate") {
        const targetTabId = action.alias ? (agentTabs[action.alias] || tabId) : tabId;
        await chrome.tabs.update(targetTabId, { url: action.url });
        if (action.url) sources.push(action.url);
        await sendTrace(queryId, "browser", "processing", `Перехожу: ${action.url?.slice(0, 60)}`);
        try {
          await waitForTabComplete(targetTabId, 15000);
        } catch {
          await log("WARN: page load timeout, continuing...");
        }
        await sleep(1000);
        if (action.url?.startsWith("http")) {
          await ensureContentScript(targetTabId);
        }
        continue;
      }

      // DOM-действия: click, type, scroll, hover, select, press, focus
      if (currentUrl.startsWith("http")) {
        await ensureContentScript(tabId);
        const actionDesc = action.type === "click" ? `Нажимаю: ${action.selector?.slice(0, 40)}`
          : action.type === "type" ? `Ввожу: ${action.text?.slice(0, 30)}`
          : action.type === "hover" ? `Навожу: ${action.selector?.slice(0, 40)}`
          : `${action.type}`;
        await sendTrace(queryId, "browser", "processing", actionDesc);
        try {
          await runActionInTab(tabId, action);
        } catch (e) {
          await log(`WARN: action failed: ${e.message}`);
        }
      }
      await sleep(300);
    }

    // Если лимит шагов исчерпан
    if (history.length >= MAX_STEPS) {
      await fetch(`${ML_SERVICE_URL}/api/browser-done/${queryId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: "Достигнут лимит шагов. Попробуй уточнить запрос.", sources }),
      });
    }

    await log("DONE (browser action)");
  } catch (e) {
    await log(`ERROR: ${e.message}`);
    await fetch(`${ML_SERVICE_URL}/api/browser-done/${queryId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer: `Ошибка: ${e.message}`, sources }),
    }).catch(() => {});
  } finally {
    isRunning = false;
    abortRequested = false;
  }
};

// ─── Выполнение плана (массив действий) ──────────────────────
const executePlan = async (actions) => {
  if (!Array.isArray(actions)) throw new Error("actions must be an array");

  // Создаём ОТДЕЛЬНУЮ вкладку
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  const tabId = tab.id;

  isRunning = true;
  abortRequested = false;
  await setStatus("busy", "running…");
  await log(`CREATED TAB: ${tabId}`);

  try {
    for (let i = 0; i < actions.length; i += 1) {
      if (abortRequested) throw new Error("Stopped by user");
      const action = actions[i] ?? {};
      const type = String(action.type ?? "");
      await log(`STEP ${i + 1}/${actions.length}: ${type}`);

      if (type === "navigate") {
        const url = String(action.url ?? "");
        if (!url) throw new Error("navigate.url is required");
        await chrome.tabs.update(tabId, { url });
        try {
          await waitForTabComplete(tabId, action.timeoutMs ?? 30_000);
        } catch {
          await log("WARN: page load timeout, continuing...");
        }
        await sleep(500);
        const currentTab = await chrome.tabs.get(tabId);
        if (currentTab.url?.startsWith("http")) {
          await ensureContentScript(tabId);
        }
        continue;
      }

      await ensureContentScript(tabId);
      try {
        await runActionInTab(tabId, action);
      } catch (e) {
        await log(`WARN: action failed: ${e.message}`);
      }
      await sleep(action.delayMs ?? 200);
    }

    await setStatus("ok", "done");
    await log("DONE");
  } finally {
    isRunning = false;
    abortRequested = false;
  }
};

const isJsonArray = (text) => {
  const t = String(text ?? "").trim();
  return t.startsWith("[") && t.endsWith("]");
};

const planWithBackend = async (prompt) => {
  const resp = await fetch(DEFAULT_PLANNER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!resp.ok) {
    throw new Error(`Planner HTTP ${resp.status}`);
  }
  const data = await resp.json();
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.actions)) return data.actions;
  throw new Error("Planner response must be an array or { actions: [] }");
};

chrome.runtime.onInstalled.addListener(async () => {
  try {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch {
    // ignore if not supported
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg?.type === "STOP") {
        abortRequested = true;
        await setStatus("err", "stopping…");
        sendResponse({ ok: true });
        return;
      }

      // RUN_BROWSER_ACTION всегда обрабатываем — сбрасываем предыдущее состояние
      if (msg?.type === "RUN_BROWSER_ACTION") {
        const prompt = String(msg.prompt ?? "").trim();
        const queryId = String(msg.queryId ?? "");
        if (!prompt || !queryId) {
          sendResponse({ ok: false, error: "Пустой промпт или queryId" });
          return;
        }
        // Сбрасываем предыдущий зависший запуск
        isRunning = false;
        abortRequested = false;
        executeBrowserAction(prompt, queryId).catch((e) => {
          setStatus("err", "error");
          log(`ERROR: ${e.message}`);
        });
        sendResponse({ ok: true });
        return;
      }

      if (isRunning) {
        sendResponse({ ok: false, error: "Уже выполняется. Нажмите Стоп." });
        return;
      }

      if (msg?.type === "RUN_PLAN") {
        const actions = msg.actions;
        executePlan(actions).catch((e) => {
          setStatus("err", "error");
          log(`ERROR: ${e.message}`);
        });
        sendResponse({ ok: true });
        return;
      }

      if (msg?.type === "RUN_PROMPT") {
        const prompt = String(msg.prompt ?? "").trim();
        if (!prompt) {
          sendResponse({ ok: false, error: "Пустой промпт" });
          return;
        }

        // Используем пошаговый агент (через /api/step)
        executeAgentLoop(prompt).catch((e) => {
          setStatus("err", "error");
          log(`ERROR: ${e.message}`);
        });
        sendResponse({ ok: true });
        return;
      }

      sendResponse({ ok: false, error: "Unknown message" });
    } catch (e) {
      await setStatus("err", "error");
      await log(`ERROR: ${e.message}`);
      sendResponse({ ok: false, error: e.message });
    }
  })();

  return true;
});
