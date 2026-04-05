const DEFAULT_PLANNER_URL = "http://localhost:8001/api/plan";
const DEFAULT_STEP_URL = "http://localhost:8001/api/step";

let isRunning = false;
let abortRequested = false;

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

// ─── Пошаговый агент (через /api/step + Gemini) ──────────────
const executeAgentLoop = async (prompt) => {
  isRunning = true;
  abortRequested = false;
  await setStatus("busy", "thinking…");

  const history = [];
  const MAX_STEPS = 10;

  try {
    // Создаём ОТДЕЛЬНУЮ вкладку для работы агента
    const tab = await chrome.tabs.create({ url: "about:blank", active: true });
    const tabId = tab.id;
    await log(`CREATED TAB: ${tabId}`);

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

      if (action.type === "navigate") {
        await chrome.tabs.update(tabId, { url: action.url });
        try {
          await waitForTabComplete(tabId, 15000);
        } catch {
          await log("WARN: page load timeout, continuing...");
        }
        await sleep(1000);
        continue;
      }

      // click/type/scroll — нужен content script
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
