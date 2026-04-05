const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const promptEl = document.getElementById("prompt");
const planEl = document.getElementById("plan");
const logEl = document.getElementById("log");
const answerSection = document.getElementById("answerSection");
const answerEl = document.getElementById("answerEl");

const runBtn = document.getElementById("runBtn");
const stopBtn = document.getElementById("stopBtn");
const runPlanBtn = document.getElementById("runPlanBtn");
const clearLogBtn = document.getElementById("clearLogBtn");
const copyLogBtn = document.getElementById("copyLogBtn");

const setStatus = (kind, text) => {
  statusDot.classList.remove("ok", "busy", "err");
  if (kind) statusDot.classList.add(kind);
  statusText.textContent = text;
};

const appendLog = (line) => {
  const now = new Date();
  const ts = now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  logEl.textContent += `${ts}  ${line}\n`;
  logEl.scrollTop = logEl.scrollHeight;
};

const send = (msg) =>
  new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message));
      else resolve(resp);
    });
  });

let typewriterTimer = null;

const typewriter = (text, delay = 18) => {
  if (typewriterTimer !== null) clearTimeout(typewriterTimer);
  answerSection.classList.remove("hidden");
  answerEl.classList.remove("done");
  answerEl.textContent = "";

  let i = 0;
  const tick = () => {
    if (i < text.length) {
      answerEl.textContent += text[i++];
      typewriterTimer = setTimeout(tick, delay);
    } else {
      answerEl.classList.add("done");
      typewriterTimer = null;
    }
  };
  tick();
};

const parsePlanJson = () => {
  const raw = planEl.value.trim();
  if (!raw) throw new Error("План пустой");
  const parsed = JSON.parse(raw);
  if (!Array.isArray(parsed)) throw new Error("План должен быть массивом действий");
  return parsed;
};

const runPrompt = async () => {
  const prompt = promptEl.value.trim();
  if (!prompt) return;
  setStatus("busy", "planning…");
  appendLog(`PROMPT: ${prompt}`);
  answerSection.classList.add("hidden");
  answerEl.textContent = "";

  try {
    const resp = await send({ type: "RUN_PROMPT", prompt });
    if (resp?.ok) {
      setStatus("busy", "running…");
    } else {
      setStatus("err", "error");
      appendLog(`ERROR: ${resp?.error ?? "unknown"}`);
    }
  } catch (e) {
    setStatus("err", "error");
    appendLog(`ERROR: ${e.message}`);
  }
};

const runPlan = async () => {
  try {
    const actions = parsePlanJson();
    setStatus("busy", "running…");
    appendLog(`PLAN: ${actions.length} actions`);
    const resp = await send({ type: "RUN_PLAN", actions });
    if (!resp?.ok) {
      setStatus("err", "error");
      appendLog(`ERROR: ${resp?.error ?? "unknown"}`);
    }
  } catch (e) {
    setStatus("err", "error");
    appendLog(`ERROR: ${e.message}`);
  }
};

runBtn.addEventListener("click", runPrompt);
runPlanBtn.addEventListener("click", runPlan);
stopBtn.addEventListener("click", () => send({ type: "STOP" }).catch(() => {}));
clearLogBtn.addEventListener("click", () => (logEl.textContent = ""));
copyLogBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(logEl.textContent);
    appendLog("LOG copied");
  } catch {
    appendLog("LOG copy failed");
  }
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "STATUS") {
    setStatus(msg.kind, msg.text);
    return;
  }
  if (msg?.type === "LOG") {
    appendLog(msg.line);
    return;
  }
  if (msg?.type === "ANSWER") {
    typewriter(msg.text);
    return;
  }
});

setStatus("ok", "ready");
