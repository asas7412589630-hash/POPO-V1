/* Setup wizard — one flow instead of three pages.
 *
 * WHAT THIS REPLACED, AND WHY. Configuring the proxy used to need:
 *   設定  → type the proxy's own Base URL and paste a key
 *   模型  → add an upstream, pick which of its models to publish, per-model profile
 *   金鑰  → create a token, copy it, go back to 設定 and paste it
 * Three pages, and the operator had to hold the relationship between them in
 * their head. The observed failure was consistent: a key with no model
 * published, or a model published against a source whose key was never saved —
 * both look like "the proxy is broken" from the client side.
 *
 * THIS FILE IS THE WHOLE SERVER-SIDE SETUP:
 *   1. 上游 API        URL + key, then 讀取模型清單  (writes nothing)
 *   2. 選模型與破甲     tick models, pick deepseek / glm / gpt-5.2 per model
 *   3. 對外或對內       one switch
 *   套用              POST /setup/apply — source, publish, mode, token, in one call
 *
 * The wizard never guesses a family from a model name: upstream ids differ per
 * provider, so the operator states it. `discover` returns a guess as a default
 * only, visibly editable.
 */

const WIZ = { models: [], mode: 'remote', sourceId: '' };

/* Server root for ADMIN calls — always this page's own origin.
 *
 * The console is served BY the proxy, so its origin IS the proxy. Following
 * cfg.base here was a genuine bug: cfg.base is what a CLIENT should connect to,
 * and pasting the upstream URL into 設定 (an easy mistake — both fields are
 * called "URL") sent every admin call to OpenAI. The symptom is a console that
 * cannot read models or keys at all, reporting format errors, because the reply
 * is an HTML error page instead of JSON.
 */
function serverRoot(){
  try {
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return location.origin;
    }
  } catch (e) { /* fall through */ }
  const typed = (cfg.base || '').replace(/\/+$/, '').replace(/\/v1$/, '');
  return typed || '';
}

const FAMILY_LABEL = {
  deepseek: 'deepseek 家族',
  glm: 'glm 家族',
  gpt: 'gpt-5.2 家族',
};

function wizOut(text, bad) {
  const el = document.querySelector('#wizLoadOut');
  if (!el) return;
  el.innerHTML = text;
  el.style.color = bad ? 'var(--bad)' : 'var(--dim)';
}

/* ---- step 1: read the upstream's model list, write nothing ---- */
document.querySelector('#btnWizLoad').onclick = async () => {
  const base_url = (document.querySelector('#wizUrl').value || '').trim().replace(/\/+$/, '');
  const api_key = (document.querySelector('#wizKey').value || '').trim();
  const btn = document.querySelector('#btnWizLoad');
  if (!base_url) { wizOut('先填上游 API 網址', true); return; }
  btn.disabled = true; btn.textContent = '讀取中…';
  wizOut('正在讀取模型清單…');
  try {
    const root = (typeof serverRoot === 'function' ? serverRoot() : location.origin);
    const r = await fetch(root + '/setup/discover', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, await authHdrAsync()),
      body: JSON.stringify({ base_url, api_key }),
    });
    const raw = await r.text();
    let d = {};
    try { d = JSON.parse(raw); } catch {
      throw new Error('這個位址回的不是 JSON（HTTP ' + r.status + '）。'
        + '通常是連到了錯誤的主機——檢查你按的這頁是不是代理本身開的。');
    }
    if (!r.ok || !d.ok) throw new Error(errText(d.error, r.status));
    WIZ.models = (d.models || []).map(m => ({
      id: m.id,
      family: m.family || '',
      enabled: false,      // nothing published until the operator ticks it
    }));
    if (!WIZ.models.length) {
      wizOut('上游回傳 0 個模型。確認網址結尾是 <code>/v1</code> 且金鑰正確。', true);
      return;
    }
    wizOut('✓ 讀到 <b>' + WIZ.models.length + '</b> 個模型。往下勾選要開放的。');
    document.querySelector('#wizModelsCard').hidden = false;
    document.querySelector('#wizModeCard').hidden = false;
    drawWizardModels();
    updateApplyBtn();
  } catch (e) {
    const m = String((e && e.message) || '');
    // Only the PROXY's own rejection should send the operator back to step 0.
    // A first version matched bare "401", which also matches the UPSTREAM's
    // "上游拒絕這個密鑰（HTTP 401）" — so a wrong upstream key told the operator
    // to go fill in the proxy key instead (measured 2026-11-05).
    if (/缺少或無效的 API Key|client\.unauthorized/i.test(m)) {
      wizOut('✗ 這個頁面不是從代理本身開的。用代理給你的網址開，管理功能才會生效。', true);
    } else {
      wizOut('✗ 讀取失敗：' + esc(netErr(e)), true);
    }
  } finally {
    btn.disabled = false; btn.textContent = '讀取模型清單';
  }
};

/* ---- step 2: per-model tick + family ---- */
function drawWizardModels() {
  const box = document.querySelector('#wizModels');
  if (!WIZ.models.length) { box.innerHTML = ''; return; }
  box.innerHTML = WIZ.models.map((m, i) => `
    <div class="wizrow${m.enabled ? ' on' : ''}">
      <label class="wiztick">
        <input type="checkbox" data-i="${i}" ${m.enabled ? 'checked' : ''}>
        <span>${esc(m.id)}</span>
      </label>
      <select data-fam="${i}" ${m.enabled ? '' : 'disabled'}>
        <option value="">— 選破甲 —</option>
        ${Object.entries(FAMILY_LABEL).map(([v, t]) =>
          `<option value="${v}"${m.family === v ? ' selected' : ''}>${esc(t)}</option>`).join('')}
      </select>
    </div>`).join('');

  box.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.onchange = () => {
      const i = Number(cb.dataset.i);
      WIZ.models[i].enabled = cb.checked;
      // A model with no family cannot be published: the armour would fall back
      // to contract-only, which is the partial armour that produces refusals.
      if (cb.checked && !WIZ.models[i].family) {
        const guess = guessFamily(WIZ.models[i].id);
        if (guess) WIZ.models[i].family = guess;
      }
      // Patch this row in place instead of re-rendering the list.
      //
      // WHY: drawWizardModels() rebuilds every row from WIZ.models. Ticking box
      // A called it, which destroyed box B's DOM node mid-click — so a second
      // tick only landed sometimes, and the counter read "已選 1" after three
      // ticks (measured 2026-11-05). Updating the row means the DOM the operator
      // is clicking on is never replaced underneath them.
      const row = cb.closest('.wizrow');
      if (row) {
        row.classList.toggle('on', cb.checked);
        const sel = row.querySelector('select');
        if (sel) {
          sel.disabled = !cb.checked;
          sel.value = WIZ.models[i].family || '';
        }
      }
      refreshWizCount();
      updateApplyBtn();
    };
  });
  box.querySelectorAll('select[data-fam]').forEach(sel => {
    sel.onchange = () => {
      WIZ.models[Number(sel.dataset.fam)].family = sel.value;
      updateApplyBtn();
    };
  });
  const n = WIZ.models.filter(m => m.enabled).length;
  const c = document.querySelector('#wizCount');
  if (c) c.textContent = '已選 ' + n + ' / ' + WIZ.models.length;
}

function guessFamily(id) {
  const low = String(id || '').toLowerCase();
  if (low.includes('glm')) return 'glm';
  if (low.includes('deepseek')) return 'deepseek';
  if (low.includes('gpt') || low.includes('luna')) return 'gpt';
  return '';
}

document.querySelector('#btnWizAll').onclick = () => {
  WIZ.models.forEach(m => { m.enabled = true; if (!m.family) m.family = guessFamily(m.id); });
  drawWizardModels(); updateApplyBtn();
};
document.querySelector('#btnWizNone').onclick = () => {
  WIZ.models.forEach(m => { m.enabled = false; });
  drawWizardModels(); updateApplyBtn();
};

/* ---- step 3: reachability ---- */
document.querySelector('#wizModeSeg').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    WIZ.mode = b.dataset.mode;
    document.querySelector('#wizModeSeg').querySelectorAll('button')
      .forEach(x => x.classList.toggle('sel', x === b));
    const h = document.querySelector('#wizModeHint');
    h.innerHTML = WIZ.mode === 'local'
      ? '任何人連到這個位址都能直接用，不用金鑰。只在你自己的裝置上、且沒開隧道時適合。'
      : '任何連到這個位址的請求都要帶金鑰。開過隧道就該用這個。';
  };
});
document.querySelector('#wizModeHint').innerHTML =
  '任何連到這個位址的請求都要帶金鑰。開過隧道就該用這個。';

function refreshWizCount() {
  const n = WIZ.models.filter(m => m.enabled).length;
  const c = document.querySelector('#wizCount');
  if (c) c.textContent = '已選 ' + n + ' / ' + WIZ.models.length;
}

function updateApplyBtn() {
  const btn = document.querySelector('#btnWizApply');
  if (!btn) return;
  const picked = WIZ.models.filter(m => m.enabled);
  const missing = picked.filter(m => !m.family);
  const ready = picked.length > 0 && missing.length === 0;
  btn.disabled = !ready;
  btn.textContent = picked.length
    ? (missing.length
        ? '還有 ' + missing.length + ' 個模型沒選破甲'
        : '套用並產生 API Key（' + picked.length + ' 個模型）')
    : '套用並產生 API Key';
}

/* ---- apply ---- */
document.querySelector('#btnWizApply').onclick = async () => {
  const btn = document.querySelector('#btnWizApply');
  const out = document.querySelector('#wizApplyOut');
  const base_url = (document.querySelector('#wizUrl').value || '').trim().replace(/\/+$/, '');
  const api_key = (document.querySelector('#wizKey').value || '').trim();
  const picked = WIZ.models.filter(m => m.enabled);
  btn.disabled = true; btn.textContent = '套用中…';
  out.innerHTML = '<span style="color:var(--dim)">正在儲存來源、發布模型、發金鑰…</span>';
  try {
    const root = (typeof serverRoot === 'function' ? serverRoot() : location.origin);
    const r = await fetch(root + '/setup/apply', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, await authHdrAsync()),
      body: JSON.stringify({
        base_url, api_key, mode: WIZ.mode, create_key: true, label: 'client',
        models: picked.map(m => ({ id: m.id, family: m.family })),
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) {
      const done = (d.steps || []).map(s => '· ' + esc(s)).join('<br>');
      out.innerHTML = '<span style="color:var(--bad)">✗ ' + esc(errText(d.error, r.status)) + '</span>'
        + (done ? '<br><span style="color:var(--dim)">' + done + '</span>' : '');
      return;
    }
    WIZ.sourceId = d.source_id || '';
    out.innerHTML = '<span style="color:var(--ok)">✓ 完成</span>'
      + '<br><span style="color:var(--dim)">' + (d.steps || []).map(s => '· ' + esc(s)).join('<br>') + '</span>';

    // The result card is the whole point: URL and key, ready to paste.
    document.querySelector('#wizResult').hidden = false;
    document.querySelector('#wizOutUrl').value = d.base_url || '';
    const keyField = document.querySelector('#wizOutKey');
    keyField.value = d.key || '（未建立，沿用既有金鑰）';
    document.querySelector('#wizOutKeyHint').innerHTML = d.key
      ? '這是唯一一次顯示完整金鑰。伺服器只存雜湊，之後無法再印出。'
      : '伺服器不保存明文，所以無法顯示舊金鑰。要一把新的就到「金鑰」頁建立。';

    // Adopt the result locally so 對話 works immediately.
    cfg.base = d.base_url || cfg.base;
    if (d.key) cfg.key = d.key;
    const first = picked[0] && picked[0].id;
    if (first) cfg.model = first;
    saveWithTrim();
    paintConn();
    if (typeof loadAccess === 'function') loadAccess();
    out.innerHTML += '<br><span style="color:var(--ok)">已同步到這台裝置，可以直接開始對話。</span>';
    document.querySelector('#wizResult').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    out.innerHTML = '<span style="color:var(--bad)">✗ ' + esc(netErr(e)) + '</span>';
  } finally {
    updateApplyBtn();
  }
};

document.querySelector('#btnWizCopy').onclick = async () => {
  const u = document.querySelector('#wizOutUrl').value;
  const k = document.querySelector('#wizOutKey').value;
  const block = 'Base URL: ' + u + '\nAPI Key:  ' + k;
  try { await navigator.clipboard.writeText(block); toast('已複製兩項'); }
  catch { toast('複製失敗，請長按選取'); }
};

/* Load the current upstream into the form so re-configuring is a diff, not a
 * retype. Failure here is silent on purpose: a fresh install has no source. */
(async function primeWizard() {
  try {
    const root = (typeof serverRoot === 'function' ? serverRoot() : location.origin);
    const r = await adminFetch('/sources');
    if (!r.ok) return;
    const d = await r.json();
    const s = (d.sources || [])[0];
    if (!s) return;
    const u = document.querySelector('#wizUrl');
    if (u && !u.value) u.value = s.base_url || '';
    if (s.configured) wizOut('已有上游來源：<b>' + esc(s.name || s.id) +
                             '</b>。要改就填新的網址與金鑰，套用會覆蓋。');
  } catch (e) { /* no source yet — expected on a new install */ }
})();
