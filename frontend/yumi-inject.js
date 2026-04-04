// ─── Юми: Hero/Mini режимы + система эмоций ─────────────────
(function () {
  const BASE = chrome.runtime.getURL("avatars/");

  // ─── Emotion map (CLAUDE.md) ──────────────────────────────
  const EMOTION_MAP = {
    welcome:    { img: 'Smile.png',     anim: 'float'  },
    idle:       { img: 'Normal.png',    anim: 'float'  },
    userTyping: { img: 'Surprised.png', anim: 'bounce' },
    thinking:   { img: 'Sleepy.png',    anim: 'pulse'  },
    searching:  { img: 'Shocked.png',   anim: 'pulse'  },
    analyzing:  { img: 'Smug.png',      anim: 'pulse'  },
    comparing:  { img: 'Annoyed.png',   anim: 'pulse'  },
    excited:    { img: 'Laugh.png',     anim: 'bounce' },
    success:    { img: 'Delighted.png', anim: 'bounce' },
    gentle:     { img: 'Smile.png',     anim: 'float'  },
    error:      { img: 'Sad.png',       anim: 'shake'  },
    retry:      { img: 'Angry.png',     anim: 'shake'  },
  };

  // ─── State ────────────────────────────────────────────────
  let currentState  = null;     // null чтобы первый setAvatar всегда сработал
  let currentMode   = 'hero';
  let resetTimer    = null;
  let typingTimer   = null;

  // ─── CSS ─────────────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    /* ── Composer fix ── */
    #yumi-composer-bg {
      position: fixed !important;
      bottom: 0 !important;
      left: 0 !important;
      right: 0 !important;
      height: var(--yumi-gap-h, 12px) !important;
      background: var(--yumi-panel-bg, #fff) !important;
      z-index: 99 !important;
      pointer-events: none !important;
    }
    [class*="composer"],[class*="query"],[class*="input-area"],
    [class*="InputArea"],[class*="QueryInput"],[class*="chat-input"],
    [class*="ChatInput"],form:has(textarea) {
      position: fixed !important;
      bottom: 12px !important;
      left: 50% !important;
      right: auto !important;
      transform: translateX(-50%) !important;
      width: calc(100% - 32px) !important;
      max-width: 600px !important;
      z-index: 100 !important;
    }

    /* ── Hero stage ── */
    #yumi-stage {
      position: fixed !important;
      /* опускаем аватар ниже области ввода, чтобы панель поиска не заезжала на него */
      bottom: calc(-1 * (var(--yumi-composer-h, 72px) - 12px)) !important;
      left: 0; right: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      pointer-events: none;
      z-index: 0;
      transition: opacity 500ms cubic-bezier(0.34,1.56,0.64,1),
                  transform 500ms cubic-bezier(0.34,1.56,0.64,1);
    }
    #yumi-stage.hidden {
      opacity: 0;
      transform: translateY(40px) scale(0.92);
    }
    #yumi-aura {
      position: absolute;
      bottom: var(--yumi-composer-h, 72px);
      left: 50%; transform: translateX(-50%);
      width: 520px; height: 400px;
      border-radius: 50%;
      filter: blur(60px);
      opacity: 0.45;
      animation: yumiAura 7s ease-in-out infinite;
    }
    #yumi-img {
      position: relative;
      z-index: 0;
      width: min(630px, 92vw);
      height: min(630px, 92vw);
      object-fit: contain;
      margin-bottom: 0;
      filter: drop-shadow(0 6px 32px rgba(244,114,182,0.25));
      transition: opacity 175ms ease, transform 175ms ease;
    }
    #yumi-img.transitioning {
      opacity: 0;
      transform: scale(0.95);
    }

    /* ── Mini avatar ── */
    #yumi-mini {
      display: none;
      align-items: center;
      justify-content: center;
      width: 48px; height: 48px;
      border-radius: 50%;
      overflow: hidden;
      cursor: pointer;
      flex-shrink: 0;
      transition: transform 200ms ease, box-shadow 200ms ease;
      position: fixed !important;
      top: 8px !important;
      left: 8px !important;
      z-index: 9999 !important;
    }
    #yumi-mini.visible { display: flex; }
    #yumi-mini:hover { transform: scale(1.12); }
    #yumi-mini img {
      width: 100%; height: 100%;
      object-fit: cover;
      transition: opacity 175ms ease, transform 175ms ease;
      border-radius: 50%;
    }
    #yumi-mini img.transitioning {
      opacity: 0;
      transform: scale(0.9);
    }
    #yumi-mini-glow {
      position: absolute;
      inset: -3px;
      border-radius: 50%;
      background: conic-gradient(
        rgba(244,114,182,0.7),
        rgba(143,189,101,0.7),
        rgba(244,114,182,0.7)
      );
      z-index: -1;
      animation: yumiMiniSpin 3s linear infinite;
      opacity: 0;
      transition: opacity 300ms;
    }
    #yumi-mini.active #yumi-mini-glow { opacity: 1; }

    /* ── Tooltip ── */
    #yumi-tooltip {
      position: fixed;
      top: 60px; left: 12px;
      background: rgba(22,28,19,0.92);
      color: #f5f0eb;
      font-size: 11px;
      font-family: 'DM Sans', sans-serif;
      padding: 6px 10px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.08);
      z-index: 999;
      pointer-events: none;
      opacity: 0;
      transform: translateY(-4px);
      transition: opacity 200ms, transform 200ms;
    }
    #yumi-tooltip.visible {
      opacity: 1;
      transform: translateY(0);
    }

    /* ── Animations ── */
    @keyframes yumiFloat {
      0%,100% { transform: translateY(0px) scale(1); }
      50%      { transform: translateY(-12px) scale(1.015); }
    }
    @keyframes yumiBounce {
      0%,100% { transform: translateY(0); }
      30%      { transform: translateY(-18px); }
      60%      { transform: translateY(-6px); }
    }
    @keyframes yumiPulse {
      0%,100% { transform: scale(1);    opacity: 1;    }
      50%      { transform: scale(1.05); opacity: 0.82; }
    }
    @keyframes yumiShake {
      0%,100% { transform: translateX(0); }
      20%      { transform: translateX(-9px); }
      40%      { transform: translateX(9px); }
      60%      { transform: translateX(-5px); }
      80%      { transform: translateX(5px); }
    }
    @keyframes yumiAura {
      0%,100% { background: radial-gradient(ellipse, rgba(244,114,182,0.55), transparent 68%); }
      50%      { background: radial-gradient(ellipse, rgba(143,189,101,0.55), transparent 68%); }
    }
    @keyframes yumiMiniSpin {
      from { transform: rotate(0deg); }
      to   { transform: rotate(360deg); }
    }

    .yumi-anim--float  { animation: yumiFloat  3s ease-in-out infinite; }
    .yumi-anim--bounce { animation: yumiBounce 0.6s ease forwards; }
    .yumi-anim--pulse  { animation: yumiPulse  1.5s ease-in-out infinite; }
    .yumi-anim--shake  { animation: yumiShake  0.5s ease forwards; }

    @media (prefers-reduced-motion: reduce) {
      #yumi-img, #yumi-aura, #yumi-mini img, #yumi-mini-glow {
        animation: none !important;
        transition: none !important;
      }
    }
  `;
  document.head.appendChild(style);

  // ─── Hero DOM ─────────────────────────────────────────────
  const stage = document.createElement('div');
  stage.id = 'yumi-stage';

  const aura = document.createElement('div');
  aura.id = 'yumi-aura';

  const heroImg = document.createElement('img');
  heroImg.id = 'yumi-img';
  heroImg.alt = 'Юми';
  heroImg.src = BASE + 'Smile.png';

  stage.appendChild(aura);
  stage.appendChild(heroImg);
  document.body.appendChild(stage);

  // Подложка под полем ввода
  const composerBg = document.createElement('div');
  composerBg.id = 'yumi-composer-bg';
  document.body.appendChild(composerBg);

  // ─── Mini DOM ─────────────────────────────────────────────
  const mini = document.createElement('div');
  mini.id = 'yumi-mini';

  const miniGlow = document.createElement('div');
  miniGlow.id = 'yumi-mini-glow';

  const miniImg = document.createElement('img');
  miniImg.alt = 'Юми';
  miniImg.src = BASE + 'Smile.png';

  mini.appendChild(miniGlow);
  mini.appendChild(miniImg);
  document.body.appendChild(mini);

  // Tooltip
  const tooltip = document.createElement('div');
  tooltip.id = 'yumi-tooltip';
  document.body.appendChild(tooltip);

  mini.addEventListener('click', () => {
    tooltip.textContent = `state: ${currentState}`;
    tooltip.classList.toggle('visible');
    setTimeout(() => tooltip.classList.remove('visible'), 2000);
  });

  // ─── Смена аватара с crossfade ────────────────────────────
  function setAvatar(state) {
    if (state === currentState) return;
    currentState = state;

    const emotion = EMOTION_MAP[state] ?? EMOTION_MAP.idle;
    const newSrc  = BASE + emotion.img;
    const newAnim = 'yumi-anim--' + emotion.anim;

    // Hero
    heroImg.classList.add('transitioning');
    setTimeout(() => {
      heroImg.src = newSrc;
      heroImg.className = newAnim + ' transitioning';
      requestAnimationFrame(() => {
        requestAnimationFrame(() => heroImg.classList.remove('transitioning'));
      });
    }, 175);

    // Mini
    miniImg.classList.add('transitioning');
    setTimeout(() => {
      miniImg.src = newSrc;
      miniImg.classList.remove('transitioning');
    }, 175);

    // Glow при активном состоянии
    const activeStates = ['thinking','searching','analyzing','comparing','error','retry'];
    mini.classList.toggle('active', activeStates.includes(state));
  }

  // ─── Переключение режимов ─────────────────────────────────
  function setMode(newMode) {
    if (currentMode === newMode) return;
    currentMode = newMode;

    if (newMode === 'mini') {
      stage.classList.add('hidden');
      mini.classList.add('visible');
    } else {
      stage.classList.remove('hidden');
      mini.classList.remove('visible');
      setAvatar('welcome');
    }
  }


  // ─── Сброс в idle после временных анимаций ───────────────
  function scheduleReset(delay = 2500) {
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      setAvatar(currentMode === 'hero' ? 'welcome' : 'idle');
    }, delay);
  }

  // ─── Публичный API через CustomEvent (CLAUDE.md Вариант C) ─
  window.addEventListener('yumi:state', (e) => {
    const state = e.detail?.state;
    if (state && EMOTION_MAP[state]) {
      setAvatar(state);
      if (['success','excited','error','retry'].includes(state)) {
        scheduleReset();
      }
    }
  });

  // ─── Хранилище фейковых результатов для React ────────────
  const fakeResults = {};

  // ─── Перехват fetch ───────────────────────────────────────
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url ?? '';
    const init = args[1] ?? {};

    // ── Перехват POST /api/query → редирект на /api/plan ──
    if ((url.includes('/api/query') || url.endsWith('/query')) &&
        (init.method ?? 'GET').toUpperCase() === 'POST') {

      setAvatar('thinking');

      try {
        // Читаем промпт из тела запроса
        let prompt = '';
        try {
          const body = typeof init.body === 'string'
            ? JSON.parse(init.body)
            : init.body;
          prompt = body?.query ?? body?.text ?? body?.prompt ?? '';
        } catch {}

        // Зовём /api/plan
        const planResp = await origFetch('http://localhost:8001/api/plan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt }),
        });

        const planData = await planResp.json();
        const actions = Array.isArray(planData) ? planData
                      : Array.isArray(planData?.actions) ? planData.actions
                      : [];

        if (actions.length > 0) {
          setAvatar('searching');
          // Выполняем actions через background.js
          chrome.runtime.sendMessage({ type: 'RUN_PLAN', actions }, (resp) => {
            if (resp?.ok) { setAvatar('success'); scheduleReset(); }
            else          { setAvatar('error');   scheduleReset(); }
          });
        }

        // Возвращаем фейковый request_id в React
        const fakeId = 'plan-' + Date.now();
        fakeResults[fakeId] = {
          id: fakeId,
          payload: {
            status: 'completed',
            trace: actions.map((a, i) => `Шаг ${i+1}: ${a.type}${a.url ? ' → ' + a.url : ''}`),
            sources: actions.filter(a => a.url).map(a => a.url),
            news: [],
            item: null,
          }
        };

        return new Response(JSON.stringify({ request_id: fakeId }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });

      } catch (e) {
        setAvatar('error');
        scheduleReset();
        return new Response(JSON.stringify({ request_id: 'plan-err' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    // ── Перехват GET /api/result/:id для фейковых ID ──────
    if (url.includes('/api/result/')) {
      const id = url.split('/api/result/').pop();
      if (fakeResults[id]) {
        return new Response(JSON.stringify(fakeResults[id]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    // ── /api/health → фейковый ok ─────────────────────────
    if (url.includes('/api/health')) {
      return new Response(JSON.stringify({ status: 'ok', time: new Date().toISOString() }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }

    // ── /api/history → пустой массив ──────────────────────
    if (url.includes('/api/history')) {
      return new Response(JSON.stringify([]), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }

    // ── Остальные запросы — без изменений ─────────────────
    return origFetch(...args);
  };

  // ─── Перехват EventSource → SSE ──────────────────────────
  const OrigEventSource = window.EventSource;
  window.EventSource = function (url, opts) {
    // Для фейковых plan-* ID — возвращаем закрытый EventSource сразу
    if (typeof url === 'string' && url.includes('/api/stream/')) {
      const id = url.split('/api/stream/').pop().split('?')[0];
      if (id.startsWith('plan-')) {
        // Имитируем completed через фейковый EventSource
        const fake = new OrigEventSource('data:text/event-stream,', opts);
        setTimeout(() => {
          fake.dispatchEvent(new MessageEvent('message', {
            data: JSON.stringify({ step: 'completed', status: 'done', detail: 'Готово!' })
          }));
        }, 300);
        return fake;
      }

      // Реальный SSE — слушаем события агента
      const es = new OrigEventSource(url, opts);
      es.addEventListener('message', (e) => {
        try {
          const data = JSON.parse(e.data);
          const step = data.step ?? '';
          const status = data.status ?? '';
          if (step === 'started'   || status === 'processing') setAvatar('thinking');
          else if (step.includes('search'))                    setAvatar('searching');
          else if (step.includes('analyz'))                    setAvatar('analyzing');
          else if (step === 'completed' || status === 'done') { setAvatar('success'); scheduleReset(); }
          else if (status === 'failed'  || status === 'error') { setAvatar('error');  scheduleReset(); }
        } catch {}
      });
      return es;
    }

    return new OrigEventSource(url, opts);
  };
  window.EventSource.prototype = OrigEventSource.prototype;

  // ─── Typing detection ─────────────────────────────────────
  document.addEventListener('input', (e) => {
    if (e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'INPUT') return;
    if (currentMode !== 'hero') return;

    if (e.target.value.trim().length > 0) {
      setAvatar('userTyping');
      clearTimeout(typingTimer);
      typingTimer = setTimeout(() => setAvatar('welcome'), 3000);
    } else {
      setAvatar('welcome');
    }
  }, true);

  // ─── Измеряем composer ────────────────────────────────────
  function measureComposer() {
    const bg = getComputedStyle(document.body).backgroundColor;
    document.documentElement.style.setProperty(
      '--yumi-panel-bg',
      (bg && bg !== 'rgba(0, 0, 0, 0)') ? bg : '#ffffff'
    );
    const candidates = ['form','[class*="composer"]','[class*="input"]','[class*="query"]','textarea'];
    for (const sel of candidates) {
      const el = document.querySelector(sel);
      if (el) {
        const rect = el.getBoundingClientRect();
        const h = window.innerHeight - rect.top;
        if (h > 40 && h < 300) {
          const fromBottom = window.innerHeight - rect.bottom;
          document.documentElement.style.setProperty('--yumi-gap-h', fromBottom + 'px');
          document.documentElement.style.setProperty('--yumi-composer-h', h + 'px');
          return;
        }
      }
    }
  }

  // ─── Hero/Mini по наличию сообщений ──────────────────────
  function detectMode() {
    measureComposer();
    // Кружок с мини-аватаром вверху убран — всегда оставляем hero внизу.
    setMode('hero');
  }

  // ─── Patch placeholder + скрыть плюс ─────────────────────
  function patchInput() {
    document.querySelectorAll('textarea, input[type="text"], input:not([type])').forEach(el => {
      if (el.placeholder !== 'Чем я могу тебе помочь?') {
        el.placeholder = 'Чем я могу тебе помочь?';
      }
    });
    document.querySelectorAll('button, [role="button"]').forEach(el => {
      if (el.textContent.trim() === '+' || el.textContent.trim() === '＋') {
        el.style.display = 'none';
      }
    });
  }

  // ─── Init ─────────────────────────────────────────────────
  const observer = new MutationObserver(detectMode);

  function init() {
    const root = document.getElementById('root');
    if (!root) { setTimeout(init, 100); return; }
    observer.observe(root, { childList: true, subtree: true });
    detectMode();
    patchInput();
    setAvatar('welcome');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
