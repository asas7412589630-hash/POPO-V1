/* Server root for the admin endpoints.
 * The console is served BY the proxy, so its own origin is always a valid
 * answer. Requiring cfg.base to be filled first made every page unreachable on
 * a fresh install — measured 2026-11-05. */
function traceRoot(){
  // Admin/read endpoints live on the server that served this page, never on
  // whatever cfg.base happens to hold (that is the CLIENT target, and it is
  // routinely mistyped as the upstream URL).
  try {
    if (location.protocol === 'http:' || location.protocol === 'https:') return location.origin;
  } catch (e) { /* fall through */ }
  const typed = (cfg.base || '').replace(/\/+$/, '').replace(/\/v1$/, '');
  return typed || '';
}


/* Message log + armour/effort controls.
 *
 * Split out from app.js so each piece stays readable. Loaded after app.js,
 * shares cfg / $ / esc / toast / show from it.
 *
 * What the log shows and why: `retries` counts the attempts the server threw
 * away, and `verdict` says what happened to them —
 *   ok        first attempt was accepted
 *   retry_ok  a retry replaced a rejected attempt  (visible "重試" marker)
 *   passed    a rejected attempt was delivered anyway (visible "錯誤放行")
 * Without those two markers a delivered refusal is indistinguishable from a
 * clean answer, which is the failure mode the whole proxy exists to avoid.
 */

const LOG = { client: '', conv: '', rows: [], open: '',
              seq: { clients: 0, log: 0, convs: 0 } };

/* ---------- effort ----------
 * One owner for the control. app.js also paints it (it renders before this file
 * loads); keeping two onclick handlers here and there meant the toggle fired
 * twice and the second one re-read a stale cfg.effort. */
function paintEffort(){
  $$('#effortSeg button').forEach(b =>
    b.classList.toggle('sel', (b.dataset.effort || 'auto') === (cfg.effort || 'auto')));
}

/* Ask the server which effort values this model accepts. A control that offers
 * a value the model rejects is worse than no control: the request 400s and the
 * user blames the model. */
async function probeEffort(){
  const out = $('#effortHint');
  const m = ($('#fModelCustom').value.trim() || $('#fModel').value || cfg.modelCustom || cfg.model || '');
  if (!traceRoot() || !m) { out.textContent = '自動：用實測最優值。'; return; }
  // Only gpt-5.2 has a thinking budget (2026-11-04). Every other family runs on
  // the model's own default, so offering a selector there is a control that
  // does nothing — and the probe used to render "強制思考 / 實測最優：max" for
  // glm, which read as "your choice is applied" when it is now ignored.
  const short = m.toLowerCase().split('/').pop();
  if (!short.includes('gpt-5.2')) {
    out.textContent = '此模型不套用思考強度（用模型預設）。只有 gpt-5.2 會採用這裡的選擇。';
    $$('#effortSeg button').forEach(b => { b.disabled = b.dataset.effort !== 'auto'; });
    return;
  }
  try {
    const r = await adminFetch('/effort?model=' + encodeURIComponent(m));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await readJson(r);
    if (!d.ok) throw new Error('bad');
    const sup = d.supported || [];
    if (!sup.length) {
      out.textContent = '此模型沒有思考強度設定（不需要選）。';
      // Only the non-auto choices are meaningless; "auto" must stay usable so a
      // previously saved value can be cleared.
      $$('#effortSeg button').forEach(b => b.disabled = b.dataset.effort !== 'auto');
      return;
    }
    $$('#effortSeg button').forEach(b => {
      const v = b.dataset.effort;
      b.disabled = v !== 'auto' && !sup.includes(v);
    });
    out.textContent = '支援：' + sup.join(' / ') +
      (d.measured ? '　實測最優：' + d.measured : '') +
      (d.mandatory ? '　（強制思考）' : '') +
      (d.resolved && d.resolved !== m ? '　→ ' + d.resolved : '');
  } catch {
    out.textContent = '無法查詢此模型的思考強度（仍會照你選的送）。';
    $$('#effortSeg button').forEach(b => b.disabled = false);
  }
}

/* ---------- client / conversation list ---------- */
async function loadClients(){
  const seq = ++LOG.seq.clients;
  const box = $('#clientList');
  if (!traceRoot()) { box.innerHTML = '<div class="hint" style="color:var(--dim);font-size:12px">讀不到伺服器位址</div>'; return; }
  try {
    const r = await adminFetch('/trace/clients?days=7');
    if (r.status === 401) {
      box.innerHTML = '<div style="color:var(--warn);font-size:12px">' +
        '需要 API Key 才能讀記錄。</div>';
      return;
    }
    const d = await readJson(r);
    if (seq !== LOG.seq.clients) return;
    const rows = d.clients || [];
    if (!rows.length) {
      box.innerHTML = '<div style="color:var(--dim);font-size:12px">最近 7 天沒有記錄</div>';
      return;
    }
    box.innerHTML = rows.map(c => `
      <div class="cli ${c.client === LOG.client ? 'sel' : ''}" data-client="${esc(c.client)}">
        <span>${esc(c.client)}</span>
        <small>${c.turns} 則 · ${c.convs.length} 段對話 · ${esc(c.last_when || '')}</small>
      </div>`).join('');
    box.querySelectorAll('.cli').forEach(el => el.onclick = () => {
      // client -> conversation -> turns. Clicking a client used to drop straight
      // into a flat list of every turn, which is unusable past a few hundred.
      LOG.client = el.dataset.client; LOG.conv = '';
      // Immediate feedback: three list fetches follow, and on a phone over a
      // tunnel that is a visible pause. Without a placeholder the old list just
      // sat there and the tap looked ignored.
      const _lg = $('#logList');
      if (_lg) _lg.innerHTML = '<div style="color:var(--dim);font-size:12px">載入中…</div>';
      const _cv = $('#convList');
      if (_cv) _cv.innerHTML = '<div style="color:var(--dim);font-size:12px">載入中…</div>';
      loadClients();
      Promise.all([loadConvs(), loadLog()]).catch(() => {});
    });
  } catch (e) {
    box.innerHTML = '<div style="color:var(--bad);font-size:12px">讀取失敗：' + esc(netErr(e)) + '</div>';
  }
}

/* ---------- records ---------- */
const KIND_LABEL = {
  refusal: '拒答', starved: '思考吃光預算', 'trivial-call': '敷衍工具呼叫',
  'script-instead-of-call': '該呼叫卻寫腳本', 'prose-instead-of-call': '該呼叫卻寫散文',
  ok: '接受', rejected: '被退'
};
const VERDICT_LABEL = {
  retry_ok: { t: '重試成功', c: 'warn' },
  passed: { t: '錯誤放行', c: 'bad' },
  ok: { t: '正常', c: 'ok' }
};

function verdictTags(r){
  const out = [];
  const v = VERDICT_LABEL[r.verdict];
  if (v) out.push(`<span class="tag ${v.c}">${esc(v.t)}</span>`);
  // The retry count gets exactly one tag. Previously an unknown verdict fell
  // back to "重試 N" and then the retries branch added it again, so every retried
  // turn showed the same badge twice.
  if (r.retries > 0) out.push(`<span class="tag warn">重試 ${r.retries}</span>`);
  const kinds = (r.kinds || []).filter(k => k && k !== 'ok');
  if (kinds.length) out.push(`<span class="tag bad">${esc([...new Set(kinds.map(k => KIND_LABEL[k] || k))].join('/'))}</span>`);
  if (r.error) out.push(`<span class="tag bad">錯誤</span>`);
  return out.join('');
}

/* Token breakdown for one turn.
 *
 * WHY THE SPLIT MATTERS: on a reasoning model, `reasoning_tokens` close to
 * `completion_tokens` means the budget went into thinking and the answer got
 * whatever was left — which is exactly how a long agent run degrades without
 * ever producing a refusal. A single "total_tokens" number hides that, so the
 * three counters are shown separately whenever the upstream reported them.
 */
function usageBlock(r){
  const pt = r.prompt_tokens, ct = r.completion_tokens, rt = r.reasoning_tokens;
  const cached = r.cached_tokens;
  const tools = r.tool_calls || 0;
  if (pt == null && ct == null && !tools) return '';
  const bits = [];
  if (pt != null) {
    bits.push('輸入 ' + Number(pt).toLocaleString());
    if (cached) bits.push('（快取 ' + Number(cached).toLocaleString() + '）');
  }
  if (ct != null) bits.push('輸出 ' + Number(ct).toLocaleString());
  if (rt) bits.push('思考 ' + Number(rt).toLocaleString());
  if (tools) {
    const names = (r.tool_names || []).filter(Boolean);
    bits.push('工具 ' + tools + ' 次' + (names.length ? '：' + names.join(', ') : ''));
  }
  const heavy = (rt && ct && rt > ct * 0.8);
  return '<div class="lbl">用量</div>'
    + '<div style="font-size:12px;color:var(--dim);line-height:1.7">'
    + esc(bits.join(' · '))
    + (heavy ? '<br><span style="color:var(--warn)">思考佔比偏高：輸出配額大多花在推理，'
             + '答覆被壓縮。這種情況先降低思考強度或提高上限。</span>' : '')
    + '</div>';
}

async function loadLog(){
  const seq = ++LOG.seq.log;
  const box = $('#logList');
  const scope = $('#logScope');
  if (!traceRoot()) { box.innerHTML = '<div style="color:var(--dim);font-size:12px">讀不到伺服器位址</div>'; return; }
  scope.textContent = LOG.client ? '· ' + LOG.client + (LOG.conv ? ' / ' + LOG.conv : '') : '· 全部';
  try {
    const q = '/trace/list?n=60&days=7' +
      (LOG.client ? '&client=' + encodeURIComponent(LOG.client) : '') +
      (LOG.conv ? '&conv=' + encodeURIComponent(LOG.conv) : '');
    const r = await adminFetch(q);
    // A 401 and an empty log look identical to the user ("沒有記錄"), and they mean
    // opposite things: one is a key problem, the other is a quiet week.
    if (r.status === 401) {
      box.innerHTML = '<div style="color:var(--warn);font-size:12px">' +
        '需要 API Key 才能讀記錄。到「設定」填上金鑰。</div>';
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await readJson(r);
    if (seq !== LOG.seq.log) return;
    LOG.rows = d.records || [];
    if (!LOG.rows.length) { box.innerHTML = '<div style="color:var(--dim);font-size:12px">沒有記錄</div>'; return; }
    box.innerHTML = LOG.rows.map(r => `
      <details class="rec" data-id="${esc(r.id)}">
        <summary>
          <span class="tag">${esc((r.when || '').slice(11, 19))}</span>
          <span class="tag acc">${esc(r.model || '?')}</span>
          ${r.final_model && r.final_model !== r.model
              ? '<span class="tag">→ ' + esc(r.final_model) + '</span>'
              : (realModel(r.model) && realModel(r.model) !== r.model
                 ? '<span class="tag">→ ' + esc(realModel(r.model)) + '</span>' : '')}
          <span class="tag">${esc(r.level || '-')}</span>
          ${verdictTags(r)}
          <span class="tag">${r.final_chars || 0} 字</span>
        </summary>
        <div class="recbody">
          <div class="lbl">使用者</div>
          <pre>${esc(r.user || '(空)')}</pre>
          <div class="lbl">回合資訊</div>
          <div style="color:var(--dim);font-size:12px;line-height:1.7">
            client ${esc(r.client || '?')} · conv ${esc(r.conv || '?')}<br>
            ${r.msgs || 0} 則訊息 · ${r.elapsed || 0}s · 要求 ${esc(r.model || '?')}
            ${r.final_model ? ' → 實際 ' + esc(r.final_model) : ''}
            ${r.retries ? ' · 重試 ' + r.retries + ' 次' : ''}
          </div>
          ${usageBlock(r)}
          <div class="lbl">完整內容（含每次嘗試）</div>
          <div data-body="${esc(r.id)}" style="color:var(--dim);font-size:12px">展開時載入…</div>
        </div>
      </details>`).join('');

    box.querySelectorAll('details.rec').forEach(el => {
      el.ontoggle = () => { if (el.open) loadRecord(el.dataset.id); };
    });
  } catch (e) {
    box.innerHTML = '<div style="color:var(--bad);font-size:12px">讀取失敗：' + esc(netErr(e)) + '</div>';
  }
}

async function loadRecord(id){
  const host = document.querySelector(`[data-body="${id}"]`);
  if (!host || host.dataset.loaded) return;
  host.textContent = '載入中…';
  try {
    const r = await adminFetch('/trace/get?id=' + encodeURIComponent(id));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await readJson(r);
    if (!d.ok) throw new Error(errText(d.error, 0));
    const rec = d.record;
    const ats = rec.attempts || [];
    const blocks = ats.map(a => `
      <div class="attempt ${a.kind === 'ok' ? 'ok' : 'bad'}">
        <div style="margin-bottom:4px">
          <span class="tag ${a.kind === 'ok' ? 'ok' : 'bad'}">#${a.n} ${esc(KIND_LABEL[a.kind] || a.kind)}</span>
          <span class="tag">${a.chars} 字</span>
          <span class="tag">HTTP ${a.status}</span>
          ${a.at !== undefined ? `<span class="tag">+${a.at}s</span>` : ''}
          ${a.note ? `<span class="tag">${esc(a.note)}</span>` : ''}
          <button class="mini" data-full="a${a.n}">看全文</button>
          <button class="mini" data-copy="a${a.n}">複製</button>
        </div>
        <pre id="body-a${a.n}">${esc(a.text || '(無文字)')}</pre>
      </div>`).join('');
    const finId = 'f0';
    host.innerHTML = (ats.length
        ? `<div style="color:var(--dim);font-size:11.5px;margin-bottom:6px">
             被丟棄的嘗試 ${ats.length} 次</div>${blocks}`
        : '<div style="color:var(--dim);font-size:12px;margin-bottom:6px">第一次就成功，沒有丟棄的嘗試</div>')
      + (rec.think && rec.think.trim()
          ? `<div class="lbl">模型思考
               <button class="mini" data-toggle-think="t0">展開</button>
               <button class="mini" data-full="t0">看全文</button>
               <button class="mini" data-copy="t0">複製</button>
               <span class="tag${(rec.think_chars || rec.think.length) > 4000 ? ' warn' : ''}">${
                 (rec.think_chars || rec.think.length).toLocaleString()} 字</span>
             </div>
             <pre id="body-t0" class="thinkpre">${esc(rec.think)}</pre>`
          : `<div class="lbl">模型思考</div>
             <div style="color:var(--dim);font-size:12px;margin-bottom:8px">
               這一輪沒有回傳思考內容（模型沒送 reasoning，或上游把它剝掉了）</div>`)
      + `<div class="lbl">實際送出
           <button class="mini" data-full="${finId}">看全文</button>
           <button class="mini" data-copy="${finId}">複製</button>
           ${rec.final_truncated ? '<span class="tag bad">已截斷</span>'
                                 : '<span class="tag">' + (rec.final_chars || 0) + ' 字</span>'}
         </div>
         <pre id="body-${finId}">${esc(rec.final || '(空)')}</pre>`;
    host.dataset.loaded = '1';

    // Full-text viewer + copy. A long answer is unreadable inside a 280px box, and
    // the whole point of the record is being able to read the answer, not a
    // scrollable excerpt of it.
    host.querySelectorAll('button[data-toggle-think]').forEach(b => b.onclick = () => {
      const pre = host.querySelector('#body-' + b.dataset.toggleThink);
      if (!pre) return;
      const open = pre.classList.toggle('open');
      b.textContent = open ? '收合' : '展開';
    });
    host.querySelectorAll('button[data-full]').forEach(b => b.onclick = () => {
      const src = host.querySelector('#body-' + b.dataset.full);
      if (src) openViewer(src.textContent);
    });
    host.querySelectorAll('button[data-copy]').forEach(b => b.onclick = async () => {
      const src = host.querySelector('#body-' + b.dataset.copy);
      if (!src) return;
      try { await navigator.clipboard.writeText(src.textContent); toast('已複製'); }
      catch { toast('複製失敗（瀏覽器限制）'); }
    });
  } catch (e) {
    host.innerHTML = '<span style="color:var(--bad)">' + esc(netErr(e)) + '</span>';
  }
}

/* ---------- conversation list (middle level) ---------- */
/* Model tags show the alias AND the real upstream id. The log stores both
 * (`model` is what the client asked for, `final_model` is what answered), so a
 * row can always be traced to the thing that actually generated the text —
 * without it, "glm" and "glm-flash" look like the same model at different speeds. */
const MODEL_TAG = m => {
  if (!m) return '<span class="tag acc">?</span>';
  const real = (typeof realModel === 'function') ? realModel(m) : '';
  const title = real && real !== m ? ' title="實際模型 ' + esc(real) + '"' : '';
  return '<span class="tag acc"' + title + '>' + esc(m) + '</span>';
};

/* What kind of conversation id is this?
 *
 * The proxy produces four shapes and they mean different things to an operator
 * reading the list:
 *   x-…  an id the client sent (X-Armor-Conv / `user`) — a real conversation
 *   c-…  derived from (system prompt + first user turn) — a heuristic
 *   u-…  the end-user field only
 *   t-…  a bare time bucket (weakest)
 * Showing the shape is the difference between "two threads look identical, is
 * that a bug?" and "these merged because the client sent no id". */
function convLabel(id){
  const s = String(id || '?');
  if (s.startsWith('x-')) return s;
  if (s.startsWith('c-')) return s + '（由內容推定）';
  if (s.startsWith('u-')) return s + '（user 欄位）';
  if (s.startsWith('t-')) return s + '（時間桶，最弱）';
  return s;
}

async function loadConvs(){
  const seq = ++LOG.seq.convs;
  const card = $('#convCard');
  const box = $('#convList');
  if (!traceRoot()) return;
  card.style.display = '';
  $('#convScope').textContent = LOG.client ? '· ' + LOG.client : '';
  if (!LOG.client) {
    card.style.display = 'none';
    return;
  }
  box.innerHTML = '<div style="color:var(--dim);font-size:12px">載入中…</div>';
  try {
    const r = await adminFetch('/trace/convs?client=' + encodeURIComponent(LOG.client));
    if (r.status === 401) {
      box.innerHTML = '<div style="color:var(--warn);font-size:12px">需要 API Key</div>';
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await readJson(r);
    if (seq !== LOG.seq.convs) return;
    const rows = d.conversations || [];
    if (!rows.length) { box.innerHTML = '<div style="color:var(--dim);font-size:12px">沒有對話</div>'; return; }
    box.innerHTML = rows.map(c => `
      <div class="rec" style="padding:10px 12px;cursor:pointer;${c.conv === LOG.conv ? 'border-color:var(--accent);background:#141f30' : ''}"
           data-conv="${esc(c.conv)}">
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:5px">
          ${c.models.map(MODEL_TAG).join('')}
          ${c.mixed_models ? '<span class="tag warn">混合模型</span>' : ''}
          <span class="tag">${c.turns} 則</span>
          ${c.retries ? '<span class="tag warn">重試 ' + c.retries + '</span>' : ''}
          ${c.errors ? '<span class="tag bad">錯誤 ' + c.errors + '</span>' : ''}
          <span style="flex:1"></span>
          <span class="tag">${esc((c.last_when || '').slice(5, 16))}</span>
          <button class="ghost" data-share="${esc(c.conv)}" style="padding:1px 8px;font-size:11px"
                  title="產生可給別人看的回看連結">分享</button>
        </div>
        <div style="font-size:12px;color:var(--dim);word-break:break-all">
          ${esc(convLabel(c.conv))}
        </div>
        ${c.first_user ? '<div style="font-size:12px;margin-top:4px;color:#b6c4d6">' +
          esc(c.first_user.slice(0, 80)) + '</div>' : ''}
      </div>`).join('');
    /* 分享鈕必須吃掉點擊，否則會連帶開啟對話；而它產生的連結帶「含思考」，
     * 因為絕大多數時候要分享的是「它為什麼這樣回答」，不是只給結論。 */
    box.querySelectorAll('[data-share]').forEach(btn => btn.onclick = ev => {
      ev.stopPropagation();
      if (typeof acctShareConv === 'function') acctShareConv(btn.dataset.share, 'think');
      else toast('帳戶功能未載入');
    });
    box.querySelectorAll('[data-conv]').forEach(el => el.onclick = () => {
      LOG.conv = el.dataset.conv;
      const _lg2 = $('#logList');
      if (_lg2) _lg2.innerHTML = '<div style="color:var(--dim);font-size:12px">載入中…</div>';
      loadConvs(); loadLog();
    });
  } catch (e) {
    box.innerHTML = '<div style="color:var(--bad);font-size:12px">' + esc(netErr(e)) + '</div>';
  }
}

$('#btnConvBack').onclick = () => { LOG.client = ''; LOG.conv = ''; loadConvs(); loadClients(); loadLog(); };
$('#btnLogRefresh').onclick = () => { loadClients(); loadConvs(); loadLog(); };
$('#btnLogAll').onclick = () => {
  LOG.client = ''; LOG.conv = ''; loadConvs(); loadClients(); loadLog();
};

/* Load the log when its tab opens, not on every boot: it is a network call to
 * the user's own proxy and should not run before they have configured it. */
const _show = show;
window.show = function(v){
  _show(v);
  if (v === 'log') { loadClients(); loadConvs(); loadLog(); }
  if (v === 'setup') { paintEffort(); probeEffort(); }
};
document.querySelectorAll('#tabs button').forEach(b => {
  b.onclick = () => window.show(b.dataset.view);
});
paintEffort();

/* Full-text viewer. A whole answer in a 280px scroll box is not "reading it".
 * This is a full-height sheet with its own copy button. */
function openViewer(text){
  const old = document.getElementById('viewer');
  if (old) old.remove();
  const d = document.createElement('div');
  d.id = 'viewer';
  d.innerHTML =
    '<div class="vhead">' +
      '<span>全文</span>' +
      '<span class="vcount">' + text.length.toLocaleString() + ' 字</span>' +
      '<span class="spacer"></span>' +
      '<button class="mini" id="vCopy">複製</button>' +
      '<button class="mini" id="vClose">關閉</button>' +
    '</div>' +
    '<div class="vbody"><pre></pre></div>';
  document.body.appendChild(d);
  d.querySelector('.vbody pre').textContent = text;

  // Three ways out, because the overlay covers the tab bar and a phone browser
  // has no visible "back" inside a fixed layer: the button, Escape, and a tap
  // on the dark margin. Without these the only exit was a page reload, which
  // reads as a hang (measured 2026-11-05).
  const close = () => {
    d.remove();
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = '';
  };
  const onKey = e => { if (e.key === 'Escape') close(); };
  d.querySelector('#vClose').onclick = close;
  d.addEventListener('click', e => { if (e.target === d) close(); });
  document.addEventListener('keydown', onKey);
  // Lock the page behind the overlay: on iOS a fixed layer over a scrollable
  // body lets the body scroll under your finger, which feels like the viewer
  // is broken.
  document.body.style.overflow = 'hidden';

  d.querySelector('#vCopy').onclick = async () => {
    try { await navigator.clipboard.writeText(text); toast('已複製'); }
    catch { toast('複製失敗（瀏覽器限制）'); }
  };
}


/* ---------- server log ---------- */
$('#btnSyslog').onclick = async () => {
  const host = $('#syslog');
  if (!cfg.base) { host.textContent = '先設定連線'; return; }
  host.textContent = '載入中…';
  try {
    const r = await adminFetch('/logs?n=200');
    if (r.status === 401) { host.innerHTML = '<span style="color:var(--warn)">需要 API Key</span>'; return; }
    const d = await readJson(r);
    const lines = d.lines || [];
    if (!lines.length) { host.textContent = '沒有日誌'; return; }
    host.innerHTML = '<pre style="max-height:340px">' + esc(lines.join('')) + '</pre>';
  } catch (e) {
    host.innerHTML = '<span style="color:var(--bad)">' + esc(netErr(e)) + '</span>';
  }
};
