// ─── Юми: Hero/Mini режимы + система эмоций ─────────────────
(function () {
  // Если скрипт инжектится повторно (роутинг/перезагрузка виджета) — чистим прошлую инстанцию,
  // чтобы Юми не "дублировалась".
  const existingIds = ['yumi-style', 'yumi-stage', 'yumi-mini', 'yumi-tooltip'];
  for (const id of existingIds) {
    const el = document.getElementById(id);
    if (el) el.remove();
  }

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
  let lastTypedAt   = 0;
  let heroSwapTimer = null;
  let miniSwapTimer = null;

  // ─── CSS ─────────────────────────────────────────────────
  const style = document.createElement('style');
  style.id = 'yumi-style';
  style.textContent = `
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
    [class*="composer"] textarea,[class*="query"] textarea,[class*="input-area"] textarea,
    [class*="InputArea"] textarea,[class*="QueryInput"] textarea,[class*="chat-input"] textarea,
    [class*="ChatInput"] textarea,form:has(textarea) textarea,
    [class*="composer"] input[type="text"],[class*="query"] input[type="text"],
    [class*="input-area"] input[type="text"],[class*="InputArea"] input[type="text"],
    [class*="QueryInput"] input[type="text"],[class*="chat-input"] input[type="text"],
    [class*="ChatInput"] input[type="text"],form:has(textarea) input[type="text"] {
      min-height: 44px !important;
      height: 44px !important;
      line-height: 44px !important;
      padding-top: 0 !important;
      padding-bottom: 0 !important;
      margin-top: 0 !important;
      margin-bottom: 0 !important;
      display: block !important;
      align-self: center !important;
      overflow: hidden !important;
      color: rgba(53, 47, 43, 0.68) !important;
    }
    [class*="composer"] textarea::placeholder,[class*="query"] textarea::placeholder,[class*="input-area"] textarea::placeholder,
    [class*="InputArea"] textarea::placeholder,[class*="QueryInput"] textarea::placeholder,[class*="chat-input"] textarea::placeholder,
    [class*="ChatInput"] textarea::placeholder,form:has(textarea) textarea::placeholder,
    [class*="composer"] input[type="text"]::placeholder,[class*="query"] input[type="text"]::placeholder,
    [class*="input-area"] input[type="text"]::placeholder,[class*="InputArea"] input[type="text"]::placeholder,
    [class*="QueryInput"] input[type="text"]::placeholder,[class*="chat-input"] input[type="text"]::placeholder,
    [class*="ChatInput"] input[type="text"]::placeholder,form:has(textarea) input[type="text"]::placeholder {
      opacity: 0.6 !important;
      color: rgba(213, 138, 176, 0.72) !important;
    }
    aside[aria-label="История чатов"] input[type="text"] {
      color: rgba(53, 47, 43, 0.68) !important;
    }
    aside[aria-label="История чатов"] input[type="text"]::placeholder {
      opacity: 0.6 !important;
      color: rgba(53, 47, 43, 0.52) !important;
    }
    @media (max-width: 950px) {
      body[data-yumi-mode="mini"] [class*="composer"],
      body[data-yumi-mode="mini"] [class*="query"],
      body[data-yumi-mode="mini"] [class*="input-area"],
      body[data-yumi-mode="mini"] [class*="InputArea"],
      body[data-yumi-mode="mini"] [class*="QueryInput"],
      body[data-yumi-mode="mini"] [class*="chat-input"],
      body[data-yumi-mode="mini"] [class*="ChatInput"],
      body[data-yumi-mode="mini"] form:has(textarea) {
        left: 150px !important;
        right: 12px !important;
        width: auto !important;
        max-width: none !important;
        transform: none !important;
      }
    }
    @media (max-width: 950px) {
      [class*="composer"],[class*="query"],[class*="input-area"],
      [class*="InputArea"],[class*="QueryInput"],[class*="chat-input"],
      [class*="ChatInput"],form:has(textarea) {
        left: 50% !important;
        right: auto !important;
        width: calc(100% - 32px) !important;
        max-width: 600px !important;
        transform: translateX(-50%) !important;
      }
    }

    /* ── Disable built-in React avatars from the bundled UI ── */
    .agent-avatar-frame,
    .agent-avatar-frame-stage,
    .agent-stage-image,
    .agent-stage-image-intro,
    .avatar-aura,
    .avatar-orb,
    .intro-stage,
    header img[src^="/avatars/"],
    main img[src^="/avatars/"] {
      display: none !important;
    }

    header h1 {
      font-size: 28px !important;
      line-height: 1 !important;
    }

    /* ── History drawer should fill the full extension height ── */
    aside.sidebar-noise[aria-label="История чатов"] {
      top: 0 !important;
      bottom: 0 !important;
      height: 100vh !important;
      max-height: 100vh !important;
    }

    /* ── Hero stage ── */
    #yumi-stage {
      position: fixed !important;
      bottom: max(-86px, calc(-1 * (var(--yumi-composer-h, 72px) - 68px))) !important;
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
      bottom: max(32px, calc(var(--yumi-composer-h, 72px) - 18px));
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
      width: min(900px, 96vw);
      height: min(900px, 96vw);
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
      width: 224px;
      height: 224px;
      border-radius: 0;
      overflow: visible;
      cursor: default;
      flex-shrink: 0;
      position: fixed !important;
      bottom: calc(var(--yumi-gap-h, 12px) - 34px) !important;
      left: -28px !important;
      z-index: 1 !important;
      pointer-events: none !important;
    }
    #yumi-mini.visible { display: flex; }
    #yumi-mini img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      transition: opacity 175ms ease, transform 175ms ease;
      filter: drop-shadow(0 8px 18px rgba(0,0,0,0.10));
    }
    #yumi-mini img.transitioning {
      opacity: 0;
      transform: scale(0.9);
    }
    #yumi-mini-glow,
    #yumi-tooltip {
      display: none !important;
    }

    /* ── Tooltip ── */
    #yumi-tooltip {
      position: fixed;
      bottom: calc(var(--yumi-gap-h, 12px) + 72px);
      left: 12px;
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
    @keyframes yumiMiniFloat {
      0%,100% { transform: translateY(0px) scale(1); }
      50%      { transform: translateY(-6px) scale(1.02); }
    }
    @keyframes yumiBounce {
      0%,100% { transform: translateY(0); }
      30%      { transform: translateY(-18px); }
      60%      { transform: translateY(-6px); }
    }
    @keyframes yumiMiniBounce {
      0%,100% { transform: translateY(0); }
      30%      { transform: translateY(-10px); }
      60%      { transform: translateY(-4px); }
    }
    @keyframes yumiPulse {
      0%,100% { transform: scale(1);    opacity: 1;    }
      50%      { transform: scale(1.05); opacity: 0.82; }
    }
    @keyframes yumiMiniPulse {
      0%,100% { transform: scale(1); opacity: 1; }
      50%      { transform: scale(1.03); opacity: 0.86; }
    }
    @keyframes yumiShake {
      0%,100% { transform: translateX(0); }
      20%      { transform: translateX(-9px); }
      40%      { transform: translateX(9px); }
      60%      { transform: translateX(-5px); }
      80%      { transform: translateX(5px); }
    }
    @keyframes yumiMiniShake {
      0%,100% { transform: translateX(0); }
      20%      { transform: translateX(-6px); }
      40%      { transform: translateX(6px); }
      60%      { transform: translateX(-3px); }
      80%      { transform: translateX(3px); }
    }
    @keyframes yumiSearchSweep {
      0%,100% { transform: translateX(0) translateY(0) scale(1.02); }
      25%      { transform: translateX(-8px) translateY(-2px) scale(1.03); }
      50%      { transform: translateX(8px) translateY(0) scale(1.04); }
      75%      { transform: translateX(-4px) translateY(2px) scale(1.03); }
    }
    @keyframes yumiAnalyzeLean {
      0%,100% { transform: rotate(0deg) scale(1.02); }
      25%      { transform: rotate(-1.6deg) scale(1.03); }
      50%      { transform: rotate(.8deg) scale(1.035); }
      75%      { transform: rotate(-.8deg) scale(1.03); }
    }
    @keyframes yumiCompareTilt {
      0%,100% { transform: rotate(0deg) translateY(0); }
      25%      { transform: rotate(-2.4deg) translateY(-3px); }
      50%      { transform: rotate(2.4deg) translateY(0); }
      75%      { transform: rotate(-1.4deg) translateY(2px); }
    }
    @keyframes yumiSuccessPop {
      0%       { transform: scale(1); }
      35%      { transform: scale(1.08); }
      65%      { transform: scale(.98); }
      100%     { transform: scale(1.03); }
    }
    @keyframes yumiMiniSuccessPop {
      0%       { transform: scale(1); }
      35%      { transform: scale(1.06); }
      65%      { transform: scale(.99); }
      100%     { transform: scale(1.02); }
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
    #yumi-mini .yumi-anim--float  { animation: yumiMiniFloat  2.8s ease-in-out infinite; }
    #yumi-mini .yumi-anim--bounce { animation: yumiMiniBounce 0.55s ease forwards; }
    #yumi-mini .yumi-anim--pulse  { animation: yumiMiniPulse  1.35s ease-in-out infinite; }
    #yumi-mini .yumi-anim--shake  { animation: yumiMiniShake  0.45s ease forwards; }

    #yumi-stage[data-state="thinking"] #yumi-aura,
    #yumi-stage[data-state="searching"] #yumi-aura,
    #yumi-stage[data-state="analyzing"] #yumi-aura,
    #yumi-stage[data-state="comparing"] #yumi-aura {
      opacity: 0.62;
      filter: blur(68px);
    }
    #yumi-stage[data-state="searching"] #yumi-img,
    #yumi-mini[data-state="searching"] img {
      animation-name: yumiSearchSweep;
      animation-duration: 1.9s;
      animation-timing-function: ease-in-out;
      animation-iteration-count: infinite;
    }
    #yumi-stage[data-state="analyzing"] #yumi-img,
    #yumi-mini[data-state="analyzing"] img {
      animation-name: yumiAnalyzeLean;
      animation-duration: 2.1s;
      animation-timing-function: ease-in-out;
      animation-iteration-count: infinite;
    }
    #yumi-stage[data-state="comparing"] #yumi-img,
    #yumi-mini[data-state="comparing"] img {
      animation-name: yumiCompareTilt;
      animation-duration: 1.6s;
      animation-timing-function: ease-in-out;
      animation-iteration-count: infinite;
    }
    #yumi-stage[data-state="success"] #yumi-aura,
    #yumi-stage[data-state="excited"] #yumi-aura {
      opacity: 0.72;
      filter: blur(74px);
    }
    #yumi-stage[data-state="success"] #yumi-img,
    #yumi-stage[data-state="excited"] #yumi-img {
      animation-name: yumiSuccessPop;
      animation-duration: 0.8s;
      animation-timing-function: ease-out;
      animation-fill-mode: forwards;
    }
    #yumi-mini[data-state="success"] img,
    #yumi-mini[data-state="excited"] img {
      animation-name: yumiMiniSuccessPop;
      animation-duration: 0.7s;
      animation-timing-function: ease-out;
      animation-fill-mode: forwards;
    }
    #yumi-stage[data-state="error"] #yumi-aura,
    #yumi-stage[data-state="retry"] #yumi-aura {
      opacity: 0.52;
      filter: blur(58px);
    }
    #yumi-stage[data-state="error"] #yumi-img,
    #yumi-stage[data-state="retry"] #yumi-img,
    #yumi-mini[data-state="error"] img,
    #yumi-mini[data-state="retry"] img {
      filter: saturate(0.92) drop-shadow(0 10px 24px rgba(190, 24, 93, 0.18));
    }

    @media (max-height: 820px) {
      #yumi-stage {
        bottom: max(-32px, calc(var(--yumi-composer-h, 72px) - 42px)) !important;
      }
      #yumi-aura {
        bottom: 12px;
        width: min(420px, 78vw);
        height: min(280px, 34vh);
      }
      #yumi-img {
        width: min(560px, 88vw, 62vh);
        height: min(560px, 88vw, 62vh);
      }
    }

    @media (max-height: 700px) {
      #yumi-stage {
        bottom: max(-22px, calc(var(--yumi-composer-h, 72px) - 32px)) !important;
      }
      #yumi-aura {
        width: min(340px, 72vw);
        height: min(220px, 28vh);
      }
      #yumi-img {
        width: min(500px, 84vw, 56vh);
        height: min(500px, 84vw, 56vh);
      }
    }

    @media (max-width: 760px) {
      #yumi-mini {
        bottom: calc(var(--yumi-gap-h, 12px) - 28px) !important;
        left: -32px !important;
      }
    }

    @media (max-width: 560px) {
      #yumi-mini {
        bottom: calc(var(--yumi-gap-h, 12px) - 20px) !important;
        left: -36px !important;
      }
    }

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

  function getBaseState() {
    return currentMode === 'hero' ? 'welcome' : 'idle';
  }

  function setTransientState(state, resetDelay = 0) {
    setAvatar(state);
    if (resetDelay > 0) scheduleReset(resetDelay);
  }

  // ─── Смена аватара с crossfade ────────────────────────────
  function setAvatar(state) {
    if (state === currentState) return;
    currentState = state;
    stage.dataset.state = state;
    mini.dataset.state = state;

    const emotion = EMOTION_MAP[state] ?? EMOTION_MAP.idle;
    const newSrc  = BASE + emotion.img;
    const newAnim = 'yumi-anim--' + emotion.anim;
    const nextHeroClass = `${newAnim} transitioning`;
    const nextMiniClass = `${newAnim} transitioning`;
    const currentHeroSrc = heroImg.getAttribute('src');
    const currentMiniSrc = miniImg.getAttribute('src');
    const heroAlreadyMatches = currentHeroSrc === newSrc;
    const miniAlreadyMatches = currentMiniSrc === newSrc;

    clearTimeout(heroSwapTimer);
    clearTimeout(miniSwapTimer);

    // Hero
    if (heroAlreadyMatches) {
      heroImg.className = newAnim;
    } else {
      heroImg.classList.add('transitioning');
      heroSwapTimer = setTimeout(() => {
        heroImg.src = newSrc;
        heroImg.className = nextHeroClass;
        requestAnimationFrame(() => {
          requestAnimationFrame(() => heroImg.classList.remove('transitioning'));
        });
      }, 175);
    }

    // Mini
    if (miniAlreadyMatches) {
      miniImg.className = newAnim;
    } else {
      miniImg.classList.add('transitioning');
      miniSwapTimer = setTimeout(() => {
        miniImg.src = newSrc;
        miniImg.className = nextMiniClass;
        requestAnimationFrame(() => {
          requestAnimationFrame(() => miniImg.classList.remove('transitioning'));
        });
      }, 175);
    }

    // Glow при активном состоянии
    const activeStates = ['thinking','searching','analyzing','comparing','error','retry'];
    mini.classList.toggle('active', activeStates.includes(state));
  }

  // ─── Переключение режимов ─────────────────────────────────
  function setMode(newMode) {
    if (currentMode === newMode) return;
    currentMode = newMode;
    document.body.dataset.yumiMode = newMode;

    if (newMode === 'mini') {
      stage.classList.add('hidden');
      mini.classList.add('visible');
      setAvatar('idle');
      return;
    }

    stage.classList.remove('hidden');
    mini.classList.remove('visible');
    setAvatar('welcome');
  }


  // ─── Сброс в idle после временных анимаций ───────────────
  function scheduleReset(delay = 2500) {
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      setAvatar(getBaseState());
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
          const step = String(data.step ?? '').toLowerCase();
          const detail = String(data.detail ?? '').toLowerCase();
          const status = String(data.status ?? '').toLowerCase();
          if (step === 'started'   || status === 'processing') setAvatar('thinking');
          else if (step.includes('search'))                    setAvatar('searching');
          else if (step.includes('analyz') || detail.includes('analyz')) setAvatar('analyzing');
          else if (step.includes('compar') || detail.includes('compar')) setAvatar('comparing');
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

    if (e.target.value.trim().length > 0) {
      lastTypedAt = Date.now();
      setAvatar('userTyping');
      clearTimeout(typingTimer);
      typingTimer = setTimeout(() => setAvatar(getBaseState()), currentMode === 'hero' ? 3000 : 1400);
    } else {
      setAvatar(getBaseState());
    }
  }, true);

  document.addEventListener('click', (e) => {
    const target = e.target instanceof Element ? e.target : null;
    if (!target) return;

    const retryButton = target.closest('button');
    if (retryButton && /повторить запрос/i.test(retryButton.textContent || '')) {
      setTransientState('retry', 1200);
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

  function detectErrorState() {
    const main = document.querySelector('main') || document.getElementById('root') || document.body;
    if (!main) return false;

    const errorText = Array.from(main.querySelectorAll('p, div, span')).some((node) => {
      const text = (node.textContent || '').trim();
      return text.includes('Не удалось выполнить запрос');
    });

    const retryButton = Array.from(main.querySelectorAll('button')).some((button) => {
      return /повторить запрос/i.test(button.textContent || '');
    });

    return errorText || retryButton;
  }

  // ─── Hero/Mini по наличию сообщений ──────────────────────
  function detectMode() {
    measureComposer();
    // Главная: `#/` → hero. Чат: `#/results` → mini.
    const hash = String(window.location.hash ?? '');
    const hashPath = (hash.startsWith('#') ? hash.slice(1) : hash).split('?')[0].split('#')[0];
    const isResultsRoute =
      hashPath === '/results' ||
      hashPath.startsWith('/results/') ||
      String(window.location.pathname ?? '') === '/results' ||
      String(window.location.pathname ?? '').startsWith('/results/');

    // В UI чаты живут в localStorage; это надёжнее, чем DOM (превью в сайдбаре может иметь похожие классы).
    let hasActiveChatMessages = false;
    try {
      const raw = window.localStorage.getItem('browser-assistant-chats');
      if (raw) {
        const data = JSON.parse(raw);
        const activeChatId = data?.activeChatId ?? null;
        const chats = Array.isArray(data?.chats) ? data.chats : [];
        const active = chats.find((c) => c?.id === activeChatId);
        hasActiveChatMessages = Array.isArray(active?.messages) && active.messages.length > 0;
      }
    } catch {}

    // fallback, если storage ещё пустой/сломался
    if (!hasActiveChatMessages) {
      const main = document.querySelector('main') || document.getElementById('root') || document.body;
      hasActiveChatMessages = !!main.querySelector('.message-in, .message-out, .assistant-msg');
    }

    const isChatThread = isResultsRoute || hasActiveChatMessages;
    setMode(isChatThread ? 'mini' : 'hero');

    if (detectErrorState()) {
      setAvatar('error');
      clearTimeout(resetTimer);
    }
  }

  // ─── Patch placeholder + скрыть плюс ─────────────────────
  function patchInput() {
    document.querySelectorAll('textarea, input[type="text"], input:not([type])').forEach(el => {
      const isHistorySearch = !!(el.closest('aside[aria-label="История чатов"]') || el.className?.toString().includes('sidebar-input'));
      const nextPlaceholder = isHistorySearch ? 'Что ищешь?' : 'С чем помочь?';
      if (el.placeholder !== nextPlaceholder) el.placeholder = nextPlaceholder;
    });
    document.querySelectorAll('button, [role="button"]').forEach(el => {
      if (el.textContent.trim() === '+' || el.textContent.trim() === '＋') {
        el.style.display = 'none';
      }
    });
  }

  function patchHeader() {
    document.querySelectorAll('header span').forEach((el) => {
      if (el.textContent.trim().startsWith('build ')) {
        el.style.display = 'none';
      }
    });
  }

  // ─── Init ─────────────────────────────────────────────────
  const observer = new MutationObserver(() => {
    patchInput();
    patchHeader();
    detectMode();
  });

  function init() {
    const root = document.getElementById('root');
    if (!root) { setTimeout(init, 100); return; }
    observer.observe(root, { childList: true, subtree: true });
    window.addEventListener('hashchange', detectMode);
    window.addEventListener('popstate', detectMode);
    document.addEventListener('focusin', patchInput);
    document.addEventListener('click', patchInput);
    document.addEventListener('input', patchInput);
    detectMode();
    patchInput();
    patchHeader();
    setAvatar('welcome');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
