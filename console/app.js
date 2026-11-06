/* Forge Client — front end only.
 *
 * Everything the app knows about the server comes from the user's own form.
 * There is no baked-in host, key or model id, and nothing is sent anywhere
 * except to the Base URL the user typed.
 *
 * The armour level is carried as the X-Armor-Level header on every request.
 * That is what makes "pick anything and it is already armoured" true: the
 * server composes the block set for that level, the client never has to know
 * which blocks those are.
 */
const LS = 'forge_client_v1';
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const DEFAULT = { base:'', key:'', model:'', modelCustom:'', level:'native',
                  effort:'auto', history:[], convId:'' };
let cfg = Object.assign({}, DEFAULT, read());

/* A conversation id this client owns.
 *
 * WHY IT IS SENT: the proxy has to group turns into a conversation for the
 * record page, and its fallback is a hash of (system prompt + first user turn).
 * Two separate chats that both open with "hi" under the same system prompt
 * therefore hash identically and appear as ONE thread — measured 2026-11-04, a
 * single "conversation" had 251 turns from unrelated chats. Sending an id the
 * client generates per conversation removes the guess entirely.
 *
 * Persisted so a page reload continues the same thread; rotated by 新對話.
 */
function newConvId(){
  const b = new Uint8Array(8);
  (crypto.getRandomValues ? crypto : { getRandomValues: a => {
    for (let i = 0; i < a.length; i++) a[i] = Math.random() * 256 | 0; return a; }})
    .getRandomValues(b);
  return 'ui-' + Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
}
if (!cfg.convId) { cfg.convId = newConvId(); save(); }

function read(){
  try {
    const v = JSON.parse(localStorage.getItem(LS) || '{}');
    if (!v || typeof v !== 'object' || Array.isArray(v)) return {};
    if (!Array.isArray(v.history)) v.history = [];
    if (!['native','full','lean'].includes(v.level)) v.level = DEFAULT.level;
    return v;
  } catch { return {}; }
}
/* Persist. Returns false when the write failed, because the previous version
 * swallowed the error: a full localStorage (long history + thinking text) made
 * every save silently do nothing while the UI still said 「已儲存」. */
function save(){
  try {
    localStorage.setItem(LS, JSON.stringify(cfg));
    return true;
  } catch (e) {
    return false;
  }
}

/* Trim the oldest turns until the profile fits. Called before reporting a
 * failure so the operator loses old history instead of losing the setting they
 * are currently editing. */
function saveWithTrim(){
  if (save()) return true;
  while (cfg.history && cfg.history.length > 2) {
    cfg.history = cfg.history.slice(Math.ceil(cfg.history.length / 2));
    if (save()) return true;
  }
  return save();
}

function normalizeBase(value){
  let raw = String(value || '').trim().replace(/\/+$/, '');
  if (!raw) return '';
  try {
    const u = new URL(raw);
    if (!['http:', 'https:'].includes(u.protocol)) throw new Error('Base URL 必須使用 http 或 https');
    if (u.username || u.password || u.search || u.hash) throw new Error('Base URL 不可含帳密、查詢參數或片段');
    return u.href.replace(/\/+$/, '');
  } catch (e) {
    if (e instanceof TypeError) throw new Error('Base URL 格式不正確');
    throw e;
  }
}

/* ---------------- views ---------------- */
function show(view){
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
  $('#chat').classList.toggle('on', view === 'chat');
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + view));
  if (view === 'setup') fillForm();
  if (view === 'about') fillAbout();
  /* 帳戶頁：每次進入都重新問身分。cookie 可能被別的分頁登出，而快取的面板
   * 會讓人對著一個已經失效的 session 按按鈕。 */
  if (view === 'account' && typeof acctLoadAll === 'function') acctLoadAll();
}
$$('#tabs button').forEach(b => b.onclick = () => show(b.dataset.view));
$('#btnMenu').onclick = () => show('setup');

/* ---------------- form ---------------- */
/* The armour level selector is gone: glm/deepseek are locked to one profile and
 * gpt-5.2 to another, so the control only ever offered choices that were
 * ignored. Each model's armour is now chosen once, in the setup wizard, when the
 * operator picks which models to publish. */

function fillForm(){
  $('#fBase').value = cfg.base || '';
  $('#fKey').value = cfg.key || '';
  $('#fModel').value = cfg.model || '';
  $('#fModelCustom').value = cfg.modelCustom || '';
  paintEffortSeg();
}

/* Thinking effort lives on the 調校 page now: it is a per-request preference,
 * not part of connecting. Kept here as the single place that paints the control
 * because both the tune page and the first render need it. */
function paintEffortSeg(){
  const seg = $('#effortSeg');
  if (!seg) return;
  seg.querySelectorAll('button').forEach(b =>
    b.classList.toggle('sel', (b.dataset.effort || 'auto') === (cfg.effort || 'auto')));
}
document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('#effortSeg button');
  if (!b) return;
  cfg.effort = b.dataset.effort || 'auto';
  saveWithTrim();
  paintEffortSeg();
  toast('思考強度：' + (cfg.effort === 'auto' ? '自動（上游最高）' : cfg.effort));
  // Tell the operator whether this model honours the choice — the control is
  // meaningless for a model that advertises no effort support at all.
  if (typeof probeEffort === 'function') probeEffort();
});

$('#btnSave').onclick = () => {
  try {
    cfg.base = normalizeBase($('#fBase').value);
    cfg.key = $('#fKey').value.trim();
    cfg.model = $('#fModel').value;
    cfg.modelCustom = $('#fModelCustom').value.trim();
    const persisted = saveWithTrim();
    paintConn();
    toast(persisted ? '已儲存' : '儲存失敗：本機空間不足或已停用');
    if (cfg.base && modelId()) show('chat');
  } catch (e) {
    toast(netErr(e) || '設定無效');
  }
};

$('#btnClear').onclick = async () => {
  if (!await ask('清除這台裝置上的所有設定與對話？')) return;
  localStorage.removeItem(LS);
  cfg = Object.assign({}, DEFAULT, { history: [], convId: newConvId(), level: 'native' });
  fillForm(); paintConn(); renderMsgs(); toast('已清除');
};

function modelId(){ return cfg.modelCustom || cfg.model || ''; }

/* Headers for every ADMIN call (logs, trace, keys, panel, policy preview).
 *
 * The proxy accepts EITHER a valid client key or the bootstrap token it hands
 * out at /setup/token. Both live here rather than in a constant: the old
 * `X-Armor-Admin: 1` was a literal in this file, so it authenticated nothing —
 * anyone who read the source could replay it (measured 2026-11-06).
 *
 * Chat calls do NOT use this — /v1/chat/completions is not an admin path. */
let _setupTok = '';

/* Base URL for this console's own proxy.
 * Cannot call serverRoot() (setup.js) — app.js is loaded first and this runs at
 * parse time. Same rule, written locally: a page served over http(s) is served
 * BY the proxy, so its origin IS the proxy; a file:// console has to use the
 * address stored in cfg. */
function _selfRoot(){
  try {
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return location.origin;
    }
  } catch (e) { /* file:// or blocked — fall through */ }
  const typed = ((window.cfg && cfg.base) || '').replace(/\/+$/, '').replace(/\/v1$/, '');
  return typed || '';
}

async function loadSetupToken(force){
  if (_setupTok && !force) return _setupTok;
  _setupTok = '';
  try {
    const r = await fetch(_selfRoot() + '/setup/token', { credentials: 'omit',
                                                         cache: 'no-store' });
    if (!r.ok) return '';
    const j = await r.json();
    _setupTok = (j && j.token) || '';
  } catch (e) {
    _setupTok = '';
  }
  return _setupTok;
}

/* adminFetch — an admin call that repairs a stale token.
 *
 * The token rotates whenever the proxy restarts with an empty key store, so a
 * console tab left open across a restart holds a dead one and every admin page
 * answers 401 until a manual reload. One retry with a fresh token costs nothing
 * and removes that failure mode entirely (found 2026-11-06 while testing). */
async function adminFetch(path, opts){
  const o = Object.assign({}, opts || {});
  const call = (h) => fetch(_selfRoot() + path,
                            Object.assign({}, o, { headers: Object.assign({}, o.headers || {}, h) }));
  let r = await call(AUTH());
  if (r.status === 401 && !cfg.key) {
    await loadSetupToken(true);
    r = await call(AUTH());
  }
  return r;
}

function AUTH(){
  const h = {};
  if (cfg.key) h['Authorization'] = 'Bearer ' + cfg.key;
  if (_setupTok) h['X-Armor-Setup'] = _setupTok;
  return h;
}

/* Same as AUTH() but guarantees the bootstrap token was attempted first.
 * Used by the admin page modules, which can run before app.js's init. */
async function authHdrAsync(){
  await loadSetupToken();
  return AUTH();
}

/* Alias kept because several page modules were written against this name.
 * It is the same object AUTH() returns — the admin header plus, when present,
 * the client key. Defined here because app.js loads first and keys.js/tune.js
 * both call it. */
function authHdr(){ return AUTH(); }

/* Human-readable reason for a failed fetch.
 *
 * "Failed to fetch" is what the browser throws for every network-level failure
 * — server stopped, wrong port, tunnel down, CORS rejected — and it tells the
 * operator nothing. Every catch block used to print it verbatim, so a dead
 * proxy looked like a broken console. Defined here because app.js loads first
 * and every other page file calls it. */
/* Turn any error shape into one readable line.
 *
 * WHY: the proxy answers with an ENVELOPE — {error:{code,message,hint}} — while
 * my own endpoints answer {ok:false, error:"string"}. Passing the envelope
 * straight into `new Error()` produced the literal text "[object Object]", which
 * is what the operator saw after filling in the upstream API. Nothing was wrong
 * with the request; the message was unreadable.
 *
 * Order: envelope message -> envelope code -> plain string -> HTTP status.
 * The hint is appended when present, because it names the fix.
 */
function errText(err, status){
  if (!err) return status ? ('HTTP ' + status) : '未知錯誤';
  if (typeof err === 'string') return err;
  if (typeof err === 'object') {
    const msg = err.message || err.error || err.detail || err.code || '';
    const hint = err.hint || '';
    if (msg && typeof msg === 'object') {
      const inner = msg.message || msg.code || '';
      return String(inner) + (hint ? '（' + hint + '）' : '');
    }
    if (msg) return String(msg) + (hint ? '（' + hint + '）' : '');
    if (status) return 'HTTP ' + status;
    return '未知錯誤';
  }
  return String(err);
}

/* Read a response as JSON, with a message that names the real problem.
 *
 * A plain `r.json()` throws "Unexpected token '<', \"<!DOCTYPE\"..." when the
 * request reached something that is not this proxy — a wrong host, a Captive
 * portal, a Cloudflare error page. That message is technically true and
 * completely useless: nothing about the token '<' tells the operator they are
 * talking to the wrong server (measured 2026-11-05).
 */
async function readJson(r){
  const raw = await r.text();
  try { return JSON.parse(raw); }
  catch (e) {
    const looksHtml = /^\s*</.test(raw);
    throw new Error(looksHtml
      ? '這個位址回的是網頁不是 JSON（HTTP ' + r.status + '）。'
        + '通常是連到錯誤的主機——請用代理自己給的網址。'
      : '回應不是有效 JSON（HTTP ' + r.status + '）：' + raw.slice(0, 80));
  }
}

function netErr(e){
  const m = String((e && e.message) || e || '');
  if (/Failed to fetch|NetworkError|Load failed/i.test(m)) {
    return '連不到代理（伺服器可能已停止、port 不對，或隧道斷了）';
  }
  if (/AbortError/.test(m)) return '已取消';
  if (/timeout|timed out/i.test(m)) return '逾時，代理沒有回應';
  return m || '未知錯誤';
}

/* ---- model naming -------------------------------------------------------
 * The alias is a convenience, not the model. "glm" is glm-5.3; "deepseek-flash"
 * is deepseek-v4.1-flash-1017. Showing only the alias makes the console unable
 * to answer the first question anyone asks when output looks wrong — which model
 * actually produced this. So the server publishes alias -> real id in /access and
 * every place that names a model shows both.
 */
const MODEL_MAP = {};          // alias -> real upstream id, filled from /access

function realModel(alias){
  if (!alias) return '';
  return MODEL_MAP[alias] || '';
}

/* "glm → glm-5.3", or just the alias when the mapping is unknown. */
function modelLabel(alias){
  const m = alias === undefined ? modelId() : alias;
  const real = realModel(m);
  return real && real !== m ? m + ' → ' + real : m;
}

function paintModels(map){
  if (!map || typeof map !== 'object') return;
  Object.assign(MODEL_MAP, map);
  const sel = $('#fModel');
  if (!sel) return;
  const cur = sel.value || cfg.model || '';
  const aliases = Object.keys(MODEL_MAP).sort();
  if (!aliases.length) return;
  sel.innerHTML = '<option value="">— 請選擇 —</option>' + aliases.map(a => {
    const real = realModel(a);
    const text = real && real !== a ? a + '  →  ' + real : a;
    return '<option value="' + esc(a) + '">' + esc(text) + '</option>';
  }).join('');
  // Keep the operator's choice even when the list arrives after they picked one,
  // and keep an out-of-list custom value from being silently replaced.
  sel.value = aliases.includes(cur) ? cur : '';
  renderModelSelect();
  paintConn();
  loadArmorBar();
}

function renderModelSelect(){
  const sel = $('#fModel'), label = $('#modelSelectLabel'), panel = $('#modelSelectPanel');
  if (!sel || !label || !panel) return;
  const selected = sel.options[sel.selectedIndex];
  const value = sel.value;
  const alias = value || '— 請選擇 —';
  const real = value ? realModel(value) : '';
  label.innerHTML = value
    ? '<span class="alias">' + esc(alias) + '</span>' + (real && real !== alias ? ' <i class="csr">→ ' + esc(real) + '</i>' : '')
    : '<span class="csr">— 請選擇模型 —</span>';
  panel.innerHTML = [...sel.options].map(o => {
    const a = o.value, r = a ? realModel(a) : '';
    return '<div class="mselect-opt' + (a === value ? ' sel' : '') + '" role="option" aria-selected="' + (a === value) + '" data-value="' + esc(a) + '">'
      + '<span class="alias">' + esc(a || '— 請選擇 —') + (a === value ? '<span class="tick">　✓</span>' : '') + '</span>'
      + (r && r !== a ? '<span class="real">' + esc(r) + '</span>' : (a ? '<span class="noreal">上游模型 ID 載入中…</span>' : ''))
      + '</div>';
  }).join('');
}

const modelSelectBtn = $('#modelSelectBtn');
const modelSelectPanel = $('#modelSelectPanel');
function closeModelSelect(){
  modelSelectPanel.hidden = true;
  modelSelectBtn.classList.remove('open');
  modelSelectBtn.setAttribute('aria-expanded', 'false');
}
modelSelectBtn.onclick = () => {
  const open = modelSelectPanel.hidden;
  modelSelectPanel.hidden = !open;
  modelSelectBtn.classList.toggle('open', open);
  modelSelectBtn.setAttribute('aria-expanded', String(open));
};
modelSelectPanel.onclick = e => {
  const opt = e.target.closest('.mselect-opt');
  if (!opt) return;
  const sel = $('#fModel');
  sel.value = opt.dataset.value;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  renderModelSelect();
  closeModelSelect();
};
document.addEventListener('click', e => {
  if (!e.target.closest('#modelSelect')) closeModelSelect();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModelSelect();
});
$('#fModel').addEventListener('change', renderModelSelect);

/* "Use this page's own address". The console is served by the proxy, so the
 * correct Base URL is almost always location.origin + /v1 — the operator should
 * not have to reconstruct it by hand. */
$('#btnFillSelf').onclick = () => {
  $('#fBase').value = location.origin + '/v1';
  toast('已填入 ' + location.origin + '/v1');
};

$('#btnTest').onclick = async () => {
  const base = ($('#fBase').value.trim() || '').replace(/\/+$/, '');
  const key = $('#fKey').value.trim();
  const model = ($('#fModelCustom').value.trim() || $('#fModel').value || '');
  const out = $('#testOut');
  if (!base) { out.textContent = '先填 Base URL'; return; }
  out.textContent = '測試中…（會真的送一次請求）';
  const t0 = Date.now();
  let normalized;
  try { normalized = normalizeBase(base); }
  catch (e) { out.textContent = e.message || 'Base URL 格式不正確'; return; }
  const root = normalized.replace(/\/v1$/, '');

  /* A REAL round trip, not a liveness probe.
   *
   * MEASURED 2026-11-04: this button only ever called /healthz, which answers
   * 200 on the proxy's own loopback socket and knows nothing about the model or
   * the upstream. Picking a model whose upstream was down — or a model id that
   * does not exist — still reported「連線與金鑰正常」. The test was measuring
   * "is the local process alive", which the operator already knows because the
   * page just loaded from it.
   *
   * Three things are worth knowing and they fail differently:
   *   1. can we reach the proxy at all   -> /healthz      (URL typo / down)
   *   2. is our key accepted             -> an admin read (401 vs 200)
   *   3. does THIS MODEL actually answer -> a real 1-token completion
   * Only (3) proves the thing the operator is about to rely on.
   */
  const steps = [];
  const render = (head) => {
    out.innerHTML = head + '<br><span style="color:var(--dim);font-size:11.5px">' +
      steps.join('<br>') + '</span>';
  };

  try {
    const r = await fetch(root + '/healthz');
    if (!r.ok) { steps.push('healthz: HTTP ' + r.status); return render(
      '<span style="color:var(--bad)">✗ 連不到代理</span>'); }
    steps.push('✓ 代理可連線（' + (Date.now() - t0) + 'ms）');
  } catch (e) {
    steps.push('healthz 失敗：' + (e.message || e));
    return render('<span style="color:var(--bad)">✗ 連不到代理</span>');
  }

  if (key) {
    try {
      const r = await fetch(root + '/access', { headers: AUTH() });
      if (r.status === 401) { steps.push('✗ 金鑰被拒（401）');
        return render('<span style="color:var(--bad)">✗ 金鑰無效</span>'); }
      steps.push('✓ 金鑰可用');
    } catch (e) {
      steps.push('金鑰檢查略過：' + (e.message || e));
    }
  } else {
    steps.push('· 未填金鑰（僅本機可用）');
  }

  if (!model) {
    steps.push('· 未選模型，無法驗證上游');
    return render('<span style="color:var(--warn)">△ 代理與金鑰正常，但沒有選模型</span>');
  }

  const t1 = Date.now();
  try {
    const r = await fetch(normalized + '/chat/completions', {
      method: 'POST',
      headers: Object.assign({
        'Content-Type': 'application/json',
        'X-Armor-Level': cfg.level || 'native'
      }, key ? { 'Authorization': 'Bearer ' + key } : {}),
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: 'Reply with the single word: ok' }],
        max_tokens: 16
      })
    });
    const raw = await r.text();
    let obj = null;
    try { obj = JSON.parse(raw); } catch {}
    if (!r.ok || (obj && obj.error)) {
      const e = parseErr(raw, r.status, r.headers);
      steps.push('✗ 模型 ' + model + '：' + (e.code || ('HTTP ' + r.status)));
      return render('<span style="color:var(--bad)">✗ 模型不可用' +
        (e.message ? '：' + esc(e.message.slice(0, 70)) : '') + '</span>');
    }
    const ch = (obj && obj.choices && obj.choices[0]) || {};
    const msg = ch.message || {};
    const text = String(msg.content || '').trim();
    const think = String(msg.reasoning || '').trim();
    const ms = Date.now() - t1;
    if (!text && !msg.tool_calls) {
      steps.push('△ 模型回了空的正文' +
        (think ? '（只有思考 ' + think.length + ' 字）' : ''));
      return render('<span style="color:var(--warn)">△ 連線正常，但模型沒吐出正文</span>');
    }
    steps.push('✓ 模型回應 ' + ms + 'ms：' + esc(text.slice(0, 40).replace(/\n/g, ' ')));
    const realModel = obj.model || '';
    const used = r.headers.get('X-Armor-Upstream-Model') || '';
    const prof = r.headers.get('X-Armor-Profile') || '';
    if (realModel && realModel !== model) steps.push('· 實際模型：' + esc(realModel));
    if (used) steps.push('· 上游模型：' + esc(used));
    if (prof) steps.push('· 破甲組合：' + esc(prof));
    render('<span style="color:var(--ok)">✓ 全部正常</span>');
  } catch (e) {
    steps.push('✗ 請求失敗：' + (e.message || e));
    render('<span style="color:var(--bad)">✗ 模型請求沒有完成</span>');
  }
};

/* ---------------- connection pill ---------------- */
function paintConn(){
  const ok = !!(cfg.base && modelId());
  const p = $('#connPill');
  p.className = 'pill' + (ok ? ' ok' : '');
  p.innerHTML = '<span class="dot ' + (ok ? 'ok' : '') + '"></span>' +
                esc(ok ? modelLabel() : '未設定');
  const hint = $('#emptyHint');
  if (!hint) return;
  hint.style.display = (cfg.history && cfg.history.length) ? 'none' : '';
  hint.innerHTML = ok
    ? '已連線 · <b>' + esc(modelLabel()) + '</b> · ' + esc(cfg.level) + '<br>直接輸入訊息開始。'
    : '尚未連線。<br>到「連線」填上你自己的 Base URL、金鑰與模型。';
}

/* ---------------- armour status bar ----------------
 *
 * Which armour a request gets is decided by the MODEL, and the two family sets
 * are not interchangeable. Putting that only on the 說明 page meant the operator
 * had to remember it and could not tell, from the conversation, whether the
 * armour was actually applied. This strip answers that in one line, and names
 * the three families that exist so there is no ambiguity about what is on offer.
 */
const ARMOR_FAMILIES = [
  { key: 'deepseek', label: 'deepseek 家族' },
  { key: 'glm',      label: 'glm 5.3 家族' },
  { key: 'gpt',      label: 'gpt-5.2' },
];

function paintArmorBar(prov){
  const box = $('#armorBarBody');
  if (!box) return;
  const models = (prov && prov.policy_models) || null;
  const cur = (modelId() || '').toLowerCase();
  // Which family is the CURRENT model in?
  const famOf = (alias) => {
    const k = String(alias || '').toLowerCase();
    if (k.includes('gpt') || k.includes('luna')) return 'gpt';
    if (k.includes('glm')) return 'glm';
    if (k.includes('deepseek')) return 'deepseek';
    return '';
  };
  const active = famOf(cur);

  if (!models) {
    box.innerHTML = ARMOR_FAMILIES.map(f =>
      `<span class="ab-chip off">${esc(f.label)}</span>`).join('')
      + '<span class="ab-note">（讀不到伺服器狀態）</span>';
    return;
  }

  box.innerHTML = ARMOR_FAMILIES.map(f => {
    // Sum the char count over the aliases that belong to this family.
    let chars = 0, prof = '';
    for (const [alias, info] of Object.entries(models)) {
      if (famOf(alias) !== f.key) continue;
      chars = Math.max(chars, info.chars || 0);
      if (!prof) prof = info.profile || '';
    }
    const on = active === f.key;
    return `<span class="ab-chip ${on ? 'on' : ''}" title="${esc(prof)}">`
      + esc(f.label) + (chars ? ` ${Math.round(chars / 100) / 10}k` : '')
      + (on ? ' ✓' : '') + '</span>';
  }).join('')
  + (active
      ? `<span class="ab-note">目前：${esc(modelLabel() || cur)}</span>`
      : '<span class="ab-note">先選模型</span>');
}

async function loadArmorBar(){
  try {
    const r = await fetch((typeof serverRoot === 'function' ? serverRoot() : location.origin) + '/healthz');
    const d = await r.json();
    paintArmorBar(d);
  } catch (e) {
    paintArmorBar(null);
  }
}

/* ---------------- rendering ---------------- */
function esc(s){
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* Minimal markdown: fenced code, inline code, bold. Richer syntax is not worth
 * the injection surface in a view that prints model output verbatim. */
function mdLite(t){
  const parts = String(t).split(/```/);
  return parts.map((p, i) => {
    if (i % 2 === 1) {
      const nl = p.indexOf('\n');
      const body = nl >= 0 ? p.slice(nl + 1) : p;
      return '<pre>' + esc(body.replace(/\n$/, '')) + '</pre>';
    }
    return esc(p)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/\n/g, '<br>');
  }).join('');
}

function bubble(role, text, meta){
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.innerHTML = (role === 'bot') ? mdLite(text) : esc(text).replace(/\n/g, '<br>');
  if (meta) {
    const m = document.createElement('div');
    m.className = 'meta';
    m.textContent = meta;
    d.appendChild(m);
  }
  return d;
}

function renderMsgs(){
  const box = $('#msgs');
  box.innerHTML = '';
  if (!cfg.history || !cfg.history.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.id = 'emptyHint';
    box.appendChild(e);
    paintConn();
    return;
  }
  for (const m of cfg.history) {
    const el = bubble(m.role === 'user' ? 'user' : 'bot', m.content, null);
    // Rebuild the thinking block from the saved copy: a reload must not lose it,
    // otherwise the log and the conversation disagree about what the model said.
    if (m.role !== 'user' && m.think) renderBot(el, m.content, m.think, false);
    if (m.role !== 'user' && m.meta) {
      const mm = document.createElement('div');
      mm.className = 'meta';
      mm.textContent = m.meta;
      el.appendChild(mm);
    }
    // The retry/pass marker must survive a reload, otherwise the history shows a
    // passed refusal as if it were a clean answer.
    if (m.role !== 'user' && m.verdict) {
      const t = verdictTag(m.verdict);
      if (t) el.insertAdjacentHTML('afterbegin', t);
    }
    box.appendChild(el);
  }
  box.scrollTop = box.scrollHeight;
}

/* One toast at a time. The previous version appended a node per call and left it
 * to a timer — two quick actions stacked two bubbles and neither animated. */
let _toastEl = null, _toastTimer = 0;
function toast(msg){
  if (!_toastEl) {
    _toastEl = document.createElement('div');
    _toastEl.className = 'toast';
    document.body.appendChild(_toastEl);
  }
  _toastEl.textContent = msg;
  // force a reflow so the class transition replays on a repeat call
  void _toastEl.offsetWidth;
  _toastEl.classList.add('on');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => _toastEl && _toastEl.classList.remove('on'), 2200);
}

/* ---------------- confirm ----------------
 * MEASURED 2026-11-04: every destructive action in this app was silently dead.
 * `if (!confirm(...)) return;` was evaluated inside the Android WebView, whose
 * WebChromeClient does not implement onJsConfirm — the call returns false
 * immediately, without showing a dialog. Deleting a key, clearing a
 * conversation, restarting the tunnel and switching to local mode all did
 * nothing and gave no error, which reads as "the button is broken".
 *
 * This is a drop-in replacement: `await ask(msg)` resolves true/false. It uses
 * the DOM, so it works wherever the app runs, and it never blocks on a native
 * dialog the host may refuse to show.
 */
function ask(message, opts){
  const o = opts || {};
  return new Promise(resolve => {
    let done = false;
    const finish = v => {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey);
      wrap.remove();
      resolve(v);
    };
    const wrap = document.createElement('div');
    wrap.className = 'modal';
    wrap.innerHTML =
      '<div class="modal-card" role="dialog" aria-modal="true">' +
        '<div class="modal-msg"></div>' +
        '<div class="modal-actions">' +
          '<button class="ghost" data-a="0">' +
            esc(o.cancel || '取消') + '</button>' +
          '<button class="primary" data-a="1">' +
            esc(o.ok || '確定') + '</button>' +
        '</div>' +
      '</div>';
    wrap.querySelector('.modal-msg').textContent = message;
    const onKey = e => {
      if (e.key === 'Escape') finish(false);
      if (e.key === 'Enter') finish(true);
    };
    wrap.querySelectorAll('button').forEach(b => {
      b.onclick = () => finish(b.dataset.a === '1');
    });
    wrap.addEventListener('click', e => { if (e.target === wrap) finish(false); });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(wrap);
  });
}

/* ---------------- send ---------------- */
const input = $('#input');
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(130, input.scrollHeight) + 'px';
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$('#btnSend').onclick = send;

let busy = false;
let abortCtl = null;

/* A turn can take 20–40s while the proxy retries levers upstream, and there was
 * no way out but reloading the page. These two helpers own the busy state so the
 * three exit paths (empty, upstream error, finally) cannot drift apart. */
function setBusy(on){
  busy = !!on;
  const s = $('#btnSend'), t = $('#btnStop');
  if (s) s.disabled = !!on;
  if (t) t.hidden = !on;
}
$('#btnStop').onclick = () => {
  if (abortCtl) { try { abortCtl.abort(); } catch { /* already settled */ } }
};
$('#btnNew').onclick = async () => {
  if (busy) { toast('先中止這一輪'); return; }
  if (!(cfg.history && cfg.history.length)) { toast('已經是新對話'); return; }
  if (!await ask('清空這一串對話？金鑰與設定都會保留。')) return;
  cfg.history = [];
  // A new conversation is a NEW id. Reusing the old one would file the new
  // thread under the previous conversation in the record page — the exact
  // confusion this id exists to prevent.
  cfg.convId = newConvId();
  saveWithTrim();
  renderMsgs();
  paintConn();
  toast('已開新對話');
};

/* Pull text out of one SSE data line, whatever shape the upstream uses.
 * Reasoning models put their thinking in `reasoning` and may leave `content`
 * empty until the very last chunk, so both fields are read. */
function deltaOf(obj){
  const ch = (obj.choices || [])[0] || {};
  const d = ch.delta || ch.message || {};
  // Two field names for the same thing: the zai/openai-shaped upstreams use
  // `reasoning`, deepseek uses `reasoning_content`. Reading only one meant
  // deepseek's thinking never appeared anywhere — measured 2026-10-31, every
  // deepseek turn reported "no thinking" while the stream carried it.
  return { text: d.content || ch.text || '',
           think: d.reasoning || d.reasoning_content || '' };
}

async function send(){
  const text = input.value.trim();
  if (!text || busy) return;
  if (!cfg.base) { show('setup'); toast('先填 Base URL'); return; }
  if (!modelId()) { show('setup'); toast('先選模型'); return; }

  setBusy(true);
  input.value = '';
  input.style.height = 'auto';

  cfg.history.push({ role: 'user', content: text });
  cfg.history = cfg.history.slice(-200);
  if (!saveWithTrim()) toast('本機儲存已滿：這輪不會留存在歷史');
  renderMsgs();

  const box = $('#msgs');
  const bot = bubble('bot', '');
  bot.textContent = '…';
  box.appendChild(bot);
  box.scrollTop = box.scrollHeight;

  // Only the last 40 turns go upstream: this is a client, and the proxy's own
  // context handling starts from whatever it is handed.
  const msgs = cfg.history.slice(-40).map(m => ({ role: m.role, content: m.content }));
  const t0 = Date.now();
  let acc = '', think = '', meta = '';
  // Declared at function scope: the buffered and streaming branches both set it,
  // and the shared tail reads it (a block-scoped copy made it undefined).
  let verdict = null;

  abortCtl = new AbortController();
  try {
    const r = await fetch(cfg.base.replace(/\/+$/, '') + '/chat/completions', {
      method: 'POST',
      signal: abortCtl.signal,
      headers: Object.assign({
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'X-Armor-Level': cfg.level || 'native',
        'X-Armor-Effort': cfg.effort || 'auto',
        // The proxy groups turns by this. Without it, two chats that both open
        // with "hi" hash to the same conversation in the record page.
        'X-Armor-Conv': cfg.convId || ''
      }, cfg.key ? { 'Authorization': 'Bearer ' + cfg.key } : {}),
      body: JSON.stringify({ model: modelId(), messages: msgs, stream: true,
                             user: cfg.convId || undefined })
    });

    if (!r.ok) {
      const raw = await r.text();
      bot.className = 'msg err';
      bot.innerHTML = errHtml(parseErr(raw, r.status, r.headers));
      throw new Error('http');
    }

    const ctype = r.headers.get('content-type') || '';
    // Header fallback first: the buffered path reports the verdict in headers,
    // and the streaming path may also carry it before the body starts.
    {
      const hr = r.headers.get('X-Armor-Retries');
      const hv = r.headers.get('X-Armor-Verdict');
      if (hr || hv) {
        verdict = { retries: Number(hr || 0), verdict: hv || '',
                    attempt_kinds: (r.headers.get('X-Armor-Attempts') || '')
                                    .split(',').filter(Boolean) };
      }
    }
    if (!ctype.includes('event-stream')) {
      // Proxy answered with a buffered JSON body (retry path). Parse it once.
      const raw = await r.text();
      let obj = null;
      try { obj = JSON.parse(raw); } catch {}
      const d = obj ? deltaOf(obj) : { text: raw, think: '' };
      acc = d.text; think = d.think;
      meta = [obj && obj.model, (Date.now() - t0) + 'ms',
              obj && obj.usage && obj.usage.total_tokens ? obj.usage.total_tokens + ' tok' : '']
             .filter(Boolean).join(' · ');
    } else {
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '', modelName = '', usage = null, upErr = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          // Server sidecar: ":armor {retries,verdict,attempt_kinds}". Sent as an
          // SSE comment so a standard parser ignores it, read here so the bubble
          // can say 重試/錯誤放行 instead of looking like a clean answer.
          if (line.startsWith(':armor ')) {
            try { verdict = JSON.parse(line.slice(7)); } catch {}
            continue;
          }
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;
          let obj;
          try { obj = JSON.parse(payload); } catch { continue; }
          // Upstream errors can arrive as a normal data event with HTTP 200, so
          // an error body must be recognised here or it reads as "empty answer".
          if (obj && obj.error) {
            upErr = JSON.stringify(obj.error).slice(0, 300);
            continue;
          }
          if (obj.model) modelName = obj.model;
          if (obj.usage) usage = obj.usage;
          const d = deltaOf(obj);
          if (d.think) think += d.think;
          if (d.text) {
            acc += d.text;
            renderBot(bot, acc, think, false);
            box.scrollTop = box.scrollHeight;
          } else if (d.think) {
            // Thinking-only delta: show the thinking as it grows. deepseek and glm
            // both spend their first seconds entirely in `reasoning`, so showing
            // nothing until content arrives makes the turn look stuck.
            renderBot(bot, '', think, true);
            box.scrollTop = box.scrollHeight;
          }
        }
      }
      if (upErr && !acc.trim()) {
        // Nothing was produced and the upstream said why: show the reason, not a
        // blank bubble. This is the difference between "the model is broken" and
        // "the upstream is at capacity, try again".
        bot.className = 'msg err';
        bot.innerHTML = errHtml(parseErr(upErr, 0, r.headers));
        bot.insertAdjacentHTML('beforeend',
          '<div style="margin-top:6px;font-size:11.5px;opacity:.7">' +
          '這次不會存進歷史，稍後可直接重送</div>');
        setBusy(false);
        box.scrollTop = box.scrollHeight;
        return;
      }
      meta = [modelName, (Date.now() - t0) + 'ms',
              usage && usage.total_tokens ? usage.total_tokens + ' tok' : '']
             .filter(Boolean).join(' · ');
    }

    // A reasoning model can spend the whole budget thinking and return an empty
    // content field. Showing the thinking is better than showing nothing.
    // Only the answer goes into `content`; the thinking is stored separately so
    // a later turn does not resend it to the model as if it were the reply.
    const finalText = acc;
    if (!finalText.trim()) {
      // An empty stream is a failure, not an answer. Measured 2026-10-31: the
      // upstream occasionally closes a stream with zero deltas (capacity), and
      // showing "(空回應)" in a normal bubble made it look like the model chose
      // to say nothing. It is an error and must read as one.
      bot.className = 'msg err';
      bot.textContent = '上游沒有回任何內容（可能是暫時容量不足）。再送一次即可，這次不會存進歷史。';
      setBusy(false);
      box.scrollTop = box.scrollHeight;
      return;
    }
    renderBot(bot, finalText, think, false);
    // Mark the bubble when this answer is not a clean first-try result. A
    // delivered refusal that looks identical to a normal answer is the exact
    // failure this proxy exists to prevent, so the UI has to say so.
    const vtag = verdictTag(verdict);
    if (vtag) bot.insertAdjacentHTML('afterbegin', vtag);
    if (meta) {
      const m = document.createElement('div');
      m.className = 'meta';
      m.textContent = meta + (verdict && verdict.retries ? ' · 重試 ' + verdict.retries + ' 次' : '');
      bot.appendChild(m);
    }
    cfg.history.push({ role: 'assistant', content: finalText, think: think || '',
                       meta: meta, verdict: verdict || null });
    cfg.history = cfg.history.slice(-200);
    if (!saveWithTrim()) toast('本機儲存已滿：長回覆可能無法保留');

  } catch (e) {
    if (e && e.name === 'AbortError') {
      bot.className = 'msg err';
      bot.textContent = '已中止';
    } else if (String(e.message) !== 'http') {
      bot.className = 'msg err';
      bot.textContent = '請求失敗：' + String(e.message || e) +
        '\n\n（檢查 Base URL、金鑰，以及是否被 CORS 擋住）';
    }
  } finally {
    abortCtl = null;
    setBusy(false);
    box.scrollTop = box.scrollHeight;
  }
}

/* Render a bot bubble: optional collapsible thinking block, then the answer.
 *
 * The thinking block is a real element, not a "思考中…" label: deepseek and glm
 * emit their reasoning as a separate stream field, and an operator auditing an
 * answer needs to read WHY the model decided that, not just what it decided.
 * It is collapsed by default so a finished answer is not buried under it, but
 * it stays in the saved history so it can be reopened later.
 */
function renderBot(el, text, think, live){
  const open = el.querySelector('.thinkbox') ? el.querySelector('.thinkbox').open : false;
  let html = '';
  if (think && think.trim()) {
    html += '<details class="thinkbox"' + (open ? ' open' : '') + '>' +
              '<summary>' + (live && !text ? '思考中…' : '思考過程') +
              ' <span class="tlen">' + think.length + ' 字</span></summary>' +
              '<div class="ttext">' + esc(think).replace(/\n/g, '<br>') + '</div>' +
            '</details>';
  }
  if (text) html += mdLite(text);
  else if (!think) html += '<span style="color:var(--dim);font-size:12.5px">…</span>';
  el.innerHTML = html;
  if (live && el.querySelector('.thinkbox')) {
    const tb = el.querySelector('.thinkbox .ttext');
    if (tb) tb.scrollTop = tb.scrollHeight;
  }
}

/* ---------------- about ---------------- */
function fillAbout(){
  // Show the concrete URL this page would use, so the instructions are not
  // abstract. The console is served by the proxy, so its own origin IS the
  // server the operator should point their client at.
  const el = $('#aboutSelfUrl');
  if (el) el.textContent = location.origin + '/v1';
  const rows = [
    ['Base URL', cfg.base || '（未設定）'],
    ['模型', modelLabel() || '（未設定）'],
    ['實際模型 id', realModel(modelId()) || '（連線後取得）'],
    ['層級', cfg.level || 'native'],
    ['金鑰', cfg.key ? '已設定（本機）' : '（未設定）'],
    ['對話則數', String((cfg.history || []).length)]
  ];
  $('#aboutKv').innerHTML = rows.map(([k, v]) =>
    '<div>' + esc(k) + '</div><div>' + esc(v) + '</div>').join('');
}

/* ---------------- boot ---------------- */
renderMsgs();
paintConn();
if (!cfg.base) show('setup'); else show('chat');

/* Pull the alias->real-id map at boot, not only when the keys page is opened:
 * the pill and the model picker both name the model, and without the map they
 * can only show the short alias ("glm") while the request goes to glm-5.3. A
 * failed fetch is silent — the alias is still a valid label. */
if (cfg.base) {
  // Deferred: loadAccess lives in keys.js, which is loaded after this file.
  window.addEventListener('load', () => {
    if (typeof loadAccess === 'function') loadAccess().catch(() => {});
  }, { once: true });
}

if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}

/* ---------------- armour preview ----------------
 * Asking the server what a model+level actually resolves to turns the level
 * buttons from a promise into a measurement. Without it, a level that composes
 * an empty block set looks exactly like one that works — until the model
 * refuses and the user has no way to tell why.
 */
const LEVEL_DESC = {
  native: '依模型自動選', full: '人格契約＋交付契約', lean: '只有交付契約'
};
/* Badge for a turn that was retried or whose rejected answer was passed anyway.
 * Empty string for a clean turn, so nothing is added to the normal case. */
function verdictTag(v){
  if (!v) return '';
  if (v.verdict === 'passed')
    return '<div class="tag bad" style="margin-bottom:8px;display:inline-block">錯誤放行 · 重試 ' +
           (v.retries || 0) + ' 次後仍送出</div>';
  if (v.verdict === 'retry_ok' || (v.retries > 0))
    return '<div class="tag warn" style="margin-bottom:8px;display:inline-block">重試 ' +
           (v.retries || 0) + ' 次後成功</div>';
  return '';
}

/* ---------------- error rendering ----------------
 * Every failure carries a taxonomy code (client.* / upstream.* / armor.* /
 * model.*), and the code decides what the bubble says and what it advises.
 * Before this, an expired key, a capacity limit and a proxy bug all read as
 * "something went wrong", so the wrong side got debugged.
 */
const ERR_KIND = {
  'client.unauthorized':   { t: '金鑰問題',  c: 'bad',  who: '你這邊' },
  'client.bad_json':       { t: '請求格式',  c: 'bad',  who: '你這邊' },
  'client.bad_request':    { t: '請求內容',  c: 'bad',  who: '你這邊' },
  'client.rate_limited':   { t: '被限流',    c: 'warn', who: '代理這邊' },
  'upstream.capacity':     { t: '上游容量',  c: 'warn', who: '上游' },
  'upstream.auth':         { t: '上游金鑰',  c: 'bad',  who: '上游' },
  'upstream.model_not_found':{t: '模型不存在',c: 'bad', who: '上游' },
  'upstream.unreachable':  { t: '連不上上游',c: 'warn', who: '上游' },
  'upstream.server_error': { t: '上游 5xx',  c: 'warn', who: '上游' },
  'upstream.empty_stream': { t: '上游空白',  c: 'warn', who: '上游' },
  'upstream.timeout':      { t: '上游逾時',  c: 'warn', who: '上游' },
  'armor.injection_failed':{ t: '破甲失敗',  c: 'bad',  who: '代理' },
  'armor.no_policy':       { t: '破甲缺失',  c: 'bad',  who: '代理' },
  'armor.unknown_level':   { t: '層級錯誤',  c: 'bad',  who: '你這邊' },
  'model.refused':         { t: '模型拒答',  c: 'warn', who: '模型' },
  'model.empty':           { t: '模型空回應',c: 'warn', who: '模型' },
};

function parseErr(raw, status, headers){
  let obj = null;
  try { obj = JSON.parse(raw); } catch {}
  let err = obj && obj.error ? obj.error : null;
  // The vendor shape nests an array and has no OpenAI fields.
  if (err && !err.message && Array.isArray(err.errors) && err.errors[0]) {
    err = { message: err.errors[0].message || '', detail: JSON.stringify(err).slice(0, 300) };
  }
  if (typeof err === 'string') err = { message: err };
  const hdrCode = headers && headers.get ? (headers.get('X-Armor-Error') || '') : '';
  const code = (err && typeof err.code === 'string' && err.code) || hdrCode || '';
  return {
    code: code,
    message: (err && err.message) || (raw ? String(raw).slice(0, 300) : ''),
    hint: (err && err.hint) || '',
    detail: (err && err.detail) || '',
    retryable: err ? err.retryable : undefined,
    status: status || (err && err.upstream_status) || 0,
  };
}

function errHtml(e){
  const k = ERR_KIND[e.code] || null;
  const label = k ? k.t : (e.code || '錯誤');
  const color = k && k.c === 'warn' ? 'var(--warn)' : 'var(--bad)';
  let h = '<div style="color:' + color + ';font-weight:600;margin-bottom:6px">' +
          esc(label) + (k ? '　<span style="opacity:.7;font-weight:400">· ' +
          esc(k.who) + '</span>' : '') + '</div>';
  if (e.code) h += '<div style="font-size:11px;opacity:.6;margin-bottom:6px">' +
                   esc(e.code) + (e.status ? ' · HTTP ' + e.status : '') + '</div>';
  if (e.message) h += '<div style="margin-bottom:6px">' + esc(e.message) + '</div>';
  if (e.hint) h += '<div style="font-size:12px;opacity:.85">→ ' + esc(e.hint) + '</div>';
  if (e.detail) h += '<details style="margin-top:8px"><summary style="cursor:pointer;' +
    'font-size:11px;opacity:.6">原始內容</summary><pre style="font-size:11px;' +
    'white-space:pre-wrap;margin:6px 0 0">' + esc(e.detail) + '</pre></details>';
  return h;
}

/* Paint the armour strip on first load, and refresh it when the model changes
 * from any page (設定, or the wizard's post-apply sync). */
async function bootConsole(){
  await loadSetupToken();          // admin calls need this on a fresh install
  loadArmorBar();
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootConsole);
} else {
  bootConsole();
}
setInterval(loadArmorBar, 30000);

/* Fire the bootstrap fetch as soon as this file is parsed. app.js is the first
 * script the shell loads, and every other page module calls AUTH() from a click
 * handler — so by the time a human can click, this has long since resolved.
 * bootConsole() awaits the same promise, so the two cannot race. */
loadSetupToken();
