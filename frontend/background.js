const DEFAULT_PLANNER_URL = "http://localhost:8001/api/plan";

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

const getActiveTab = async () => {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) throw new Error("Нет активной вкладки");
  return tab;
};

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

const executePlan = async (actions) => {
  if (!Array.isArray(actions)) throw new Error("actions must be an array");
  const tab = await getActiveTab();
  const tabId = tab.id;

  isRunning = true;
  abortRequested = false;
  await setStatus("busy", "running…");
  await log(`ACTIVE TAB: ${tab.url ?? "(unknown)"}`);

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
        await waitForTabComplete(tabId, action.timeoutMs ?? 30_000);
        await sleep(150);
        continue;
      }

      await runActionInTab(tabId, action);
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

        await setStatus("busy", "planning…");

        let actions = null;
        if (isJsonArray(prompt)) {
          actions = JSON.parse(prompt);
        } else {
          actions = await planWithBackend(prompt);
        }

        await log(`PLANNED: ${actions.length} actions`);
        executePlan(actions).catch((e) => {
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

