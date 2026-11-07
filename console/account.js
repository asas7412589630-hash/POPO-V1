/* 帳戶層前端：登入、帳戶管理、金鑰歸屬、回看連結。
 *
 * 三個刻意的邊界：
 *   1. 密碼只在登入表單出現一次，之後只存在瀏覽器送出的表單裡。前端不存。
 *   2. session 是 HttpOnly cookie，前端讀不到，也不該讀 —— 好處是 XSS 偷不走。
 *   3. 金鑰明文只在「建立」的回應裡出現一次，之後清單只有 prefix。
 *
 * 所有寫入都帶 X-Armor-Admin: 1（跨來源頁面無法送出這個標頭）。
 */

const ACCT = { me: null, users: [], keys: [], views: [], sessions: [],
               usage: [], convs: [], settings: null, oneShot: null,
               loading: false };

function acctRoot(){
  try {
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return '';
    }
  } catch (e) {}
  return (cfg.base || '').replace(/\/+$/, '').replace(/\/v1$/, '');
}

/* 空字串 = 同源相對路徑。控制台就是代理自己服務的（/app/），所以 /a/* 用相對
 * 路徑最正確：不需要使用者先填 Base URL 才能登入，也不會被 cfg.base 指到別的
 * 主機（那是「對話要送去哪」的設定，不是「我是誰」的）。 */

function acctFetch(path, opts){
  const o = Object.assign({ credentials: 'same-origin' }, opts || {});
  o.headers = Object.assign({ 'X-Armor-Admin': '1' }, o.headers || {});
  if (o.body && !o.headers['Content-Type']) o.headers['Content-Type'] = 'application/json';
  return fetch(acctRoot() + path, o);
}

/* ------------------------------------------------------------------ 登入 */
function acctOverlay(show, html){
  let ov = $('#acctOv');
  if (!ov){
    ov = document.createElement('div');
    ov.id = 'acctOv';
    ov.style.cssText = 'position:fixed;inset:0;z-index:9000;display:none;' +
      'background:rgba(6,9,13,.94);backdrop-filter:blur(6px);padding:24px 16px;' +
      'overflow:auto';
    document.body.appendChild(ov);
  }
  ov.style.display = show ? 'block' : 'none';
  if (html !== undefined) ov.innerHTML = html;
  return ov;
}

const _tr = (k, fb) => (window.I18N && window.I18N.t ? window.I18N.t(k, fb) : fb);

function acctLoginScreen(bootstrap){
  acctOverlay(true, `
    <div style="max-width:380px;margin:8vh auto 0">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <h2 style="margin:0">${bootstrap ? _tr('account.modal_title_bootstrap', '建立擁有者帳戶') : _tr('account.modal_title_login', '登入')}</h2>
          <button class="ghost" id="acctClose" style="padding:2px 8px;font-size:16px;cursor:pointer" title="${_tr('account.btn_close', '關閉')}">✕</button>
        </div>
        <div class="hint" style="margin-bottom:14px;line-height:1.7">
          ${bootstrap
            ? _tr('account.hint_bootstrap', '這個代理還沒有任何帳戶。建立第一個 —— 它就是擁有者，可以再開其他帳戶。')
            : _tr('account.hint_login', '帳戶只存在你自己的代理裡，密碼不會上傳到任何地方。')}
        </div>
        <div class="field">
          <label>${_tr('account.username', '帳號')}</label>
          <input id="acctName" autocomplete="username" placeholder="${bootstrap ? 'owner' : _tr('account.username', '帳號')}" value="${bootstrap ? 'owner' : ''}">
        </div>
        <div class="field">
          <label>${_tr('account.password', '密碼')}</label>
          <input id="acctPass" type="password" autocomplete="${bootstrap ? 'new-password' : 'current-password'}" placeholder="${_tr('account.err_short_pw', '密碼至少 8 個字元')}">
        </div>
        <button class="primary" id="acctGo" style="width:100%">${bootstrap ? _tr('account.btn_bootstrap', '建立並登入') : _tr('account.btn_login', '登入')}</button>
        <button class="ghost" id="acctSkip" style="width:100%;margin-top:8px">${bootstrap ? _tr('account.btn_skip', '稍後設定，直接使用控制台') : _tr('account.btn_close', '關閉')}</button>
        <div id="acctErr" style="color:var(--bad);font-size:12px;margin-top:10px"></div>
        <div class="hint" style="margin-top:14px;font-size:11.5px">
          也可以在終端機用 <code>acct init</code> / <code>python triad.py acct init</code> 建立。
        </div>
      </div>
    </div>`);
  const dismiss = () => acctOverlay(false);
  const closeBtn = $('#acctClose');
  if (closeBtn) closeBtn.onclick = dismiss;
  const skipBtn = $('#acctSkip');
  if (skipBtn) skipBtn.onclick = dismiss;
  const go = async () => {
    const name = ($('#acctName').value || '').trim() || (bootstrap ? 'owner' : '');
    const pw = $('#acctPass').value || '';
    const err = $('#acctErr');
    err.textContent = '';
    if (!name) { err.textContent = _tr('account.err_missing_name', '請填帳號'); return; }
    if (pw.length < 8) { err.textContent = _tr('account.err_short_pw', '密碼至少 8 個字元'); return; }
    $('#acctGo').disabled = true;
    try {
      const r = await acctFetch('/a/login', {
        method: 'POST', body: JSON.stringify({ name, password: pw, bootstrap: !!bootstrap }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      acctOverlay(false);
      await acctLoadAll();
      toast('已登入：' + ((d.user || {}).name || name));
    } catch (e) {
      err.textContent = e.message || String(e);
    } finally {
      $('#acctGo').disabled = false;
    }
  };
  $('#acctGo').onclick = go;
  $('#acctPass').onkeydown = ev => { if (ev.key === 'Enter') go(); };
  $('#acctName').onkeydown = ev => { if (ev.key === 'Enter') go(); };
}

async function acctLoadAll(){
  ACCT.loading = true;
  try {
    const r = await acctFetch('/a/me');
    const d = await r.json().catch(() => ({}));
    ACCT.me = d.authenticated ? d : null;
    if (!d.authenticated){
      acctLoginScreen(!!d.bootstrap);
      acctPaintTab();
      return;
    }
    ACCT.keys = d.keys || [];
    ACCT.views = d.views || [];
    if (ACCT.me.is_owner) { await acctLoadUsers(); await acctLoadSettings(); }
    await Promise.all([acctLoadSessions(), acctLoadUsage(), acctLoadConvs()]);
    acctPaintTab();
  } catch (e) {
    ACCT.me = null;
  } finally {
    ACCT.loading = false;
  }
}

async function acctLoadUsers(){
  try {
    const r = await acctFetch('/a/users');
    if (!r.ok) return;
    const d = await r.json();
    ACCT.users = d.users || [];
  } catch (e) { /* 保持舊值 */ }
}

async function acctLoadSettings(){
  try {
    const r = await acctFetch('/a/settings');
    if (!r.ok) return;
    ACCT.settings = (await r.json()).settings || {};
  } catch (e) {}
}

async function acctLoadSessions(){
  try {
    const r = await acctFetch('/a/sessions');
    if (!r.ok) return;
    ACCT.sessions = (await r.json()).sessions || [];
  } catch (e) {}
}

async function acctLoadUsage(){
  try {
    const r = await acctFetch('/a/usage?days=14');
    if (!r.ok) return;
    ACCT.usage = (await r.json()).rows || [];
  } catch (e) {}
}

async function acctLoadConvs(){
  try {
    const r = await acctFetch('/a/conv?days=7');
    if (!r.ok) return;
    ACCT.convs = (await r.json()).convs || [];
  } catch (e) {}
}

/* ------------------------------------------------------------------ 面板 */
function acctFmtTs(t){
  if (!t) return '-';
  try { return new Date(t * 1000).toLocaleString('zh-TW', { hour12: false }); }
  catch (e) { return '-'; }
}

function acctPaintTab(){
  const box = $('#acctBody');
  if (!box) return;
  if (!ACCT.me){
    box.innerHTML = '<div class="card"><h2>未登入</h2>' +
      '<div class="hint">這個分頁需要登入。</div>' +
      '<button class="primary" id="acctOpenLogin" style="width:100%;margin-top:12px">登入 / 建立帳戶</button></div>';
    const b = $('#acctOpenLogin');
    if (b) b.onclick = () => acctLoadAll();
    return;
  }
  const u = ACCT.me.user || {};
  const today = ACCT.me.usage_today || {};
  const parts = [];

  parts.push(`<div class="card">
    <h2>${esc(u.name || '')} <span class="tag">${esc(u.role || '')}</span></h2>
    <div class="kv" style="margin-top:10px">
      <div>帳戶 id</div><div><code>${esc(u.id || '')}</code></div>
      <div>今日</div><div>${today.turns || 0} 輪 · ${(today.tokens_in || 0) + (today.tokens_out || 0)} tokens</div>
      <div>上次登入</div><div>${esc(acctFmtTs(u.last_login))}</div>
    </div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="ghost" id="acctLogout" style="flex:1">登出</button>
      <button class="ghost" id="acctPw" style="flex:1">改密碼</button>
    </div>
    <div id="acctPwForm" style="margin-top:12px;display:none">
      <div class="field"><label>舊密碼</label><input id="acctOldPw" type="password" autocomplete="current-password"></div>
      <div class="field"><label>新密碼（至少 8 碼）</label><input id="acctNewPw" type="password" autocomplete="new-password"></div>
      <button class="primary" id="acctPwGo" style="width:100%">更新並重新登入</button>
    </div>
  </div>`);

  /* 金鑰 */
  parts.push(`<div class="card">
    <h2>我的 API 金鑰 <span style="color:var(--dim);font-weight:400;text-transform:none">${ACCT.keys.length} 把</span></h2>
    <div class="hint" style="margin-bottom:10px">金鑰就是這個帳戶的存取憑證。明文只在建立時顯示一次。</div>
    <div class="field"><label>標籤</label><input id="acctKeyLabel" placeholder="例如：手機 / 筆電" autocomplete="off"></div>
    <button class="primary" id="acctMint" style="width:100%">建立金鑰</button>
    <div id="acctMintOut" style="margin-top:10px"></div>
    <div style="margin-top:12px">
      ${ACCT.keys.length ? ACCT.keys.map(k => `
        <div class="rec" style="padding:10px 12px;margin-bottom:8px">
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:5px">
            <span class="tag">${esc(k.prefix || '')}</span>
            <span class="tag ${k.enabled ? 'ok' : 'bad'}">${k.enabled ? '啟用' : '停用'}</span>
            <span class="tag">${k.hits || 0} 次</span>
            <span style="flex:1"></span>
            <button class="ghost" data-revoke="${esc(k.id)}" style="padding:3px 10px;font-size:11px">撤銷</button>
          </div>
          <div style="font-size:12px;color:var(--dim)">
            ${esc(k.label || '（無標籤）')} · 建立 ${esc(acctFmtTs(k.created))}
            ${k.last_used ? ' · 最後使用 ' + esc(acctFmtTs(k.last_used)) : ''}
          </div>
        </div>`).join('') : '<div class="hint">還沒有金鑰。</div>'}
    </div>
  </div>`);

  /* 回看連結 */
  parts.push(`<div class="card">
    <h2>回看連結 <span style="color:var(--dim);font-weight:400;text-transform:none">${ACCT.views.length} 個</span></h2>
    <div class="hint" style="margin-bottom:10px;line-height:1.7">
      給別人看某段對話的網址。內容由這台機器渲染，「含思考/重試」版本會連被丟棄的失敗嘗試一起顯示。
      連結可隨時撤銷 —— 撤銷後對方立刻打不開。
    </div>
    <div class="field"><label>對話</label><select id="acctViewConv">
      ${ACCT.convs.length
        ? ACCT.convs.map(c => `<option value="${esc(c.conv)}">${esc((c.conv || '').slice(0, 28))} · ${c.turns} 輪 · ${esc((c.model || ''))}</option>`).join('')
        : '<option value="">（最近 7 天沒有對話）</option>'}
    </select></div>
    <div class="field"><label>內容</label><select id="acctViewKind">
      <option value="chat">只有對話</option>
      <option value="think">對話 + 思考 + 重試</option>
    </select></div>
    <div class="field"><label>有效期限</label><select id="acctViewTtl">
      <option value="86400">1 天</option>
      <option value="604800" selected>7 天</option>
      <option value="2592000">30 天</option>
    </select></div>
    <button class="primary" id="acctNewView" style="width:100%">產生連結</button>
    <div id="acctViewOut" style="margin-top:10px"></div>
    <div style="margin-top:12px">
      ${ACCT.views.length ? ACCT.views.map(v => `
        <div class="rec" style="padding:10px 12px;margin-bottom:8px">
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:5px">
            <span class="tag ${v.kind === 'think' ? '' : 'ok'}">${v.kind === 'think' ? '含思考' : '對話'}</span>
            <span class="tag">${v.hits || 0} 次點閱</span>
            ${v.expired ? '<span class="tag bad">已過期</span>' : ''}
            <span style="flex:1"></span>
            <button class="ghost" data-copy="${esc(v.url || '')}" style="padding:3px 10px;font-size:11px">複製</button>
            <button class="ghost" data-rmview="${esc(v.vid)}" style="padding:3px 10px;font-size:11px">撤銷</button>
          </div>
          <div style="font-size:11.5px;color:var(--dim);word-break:break-all">
            ${esc((v.conv || '').slice(0, 40))} · 到期 ${esc(acctFmtTs(v.exp))}
          </div>
          <div style="font-size:11.5px;color:#7f8fa1;word-break:break-all;margin-top:3px">${esc(v.url || '')}</div>
        </div>`).join('') : '<div class="hint">還沒有連結。</div>'}
    </div>
  </div>`);

  /* 用量 */
  const maxTurn = Math.max(1, ...ACCT.usage.map(r => r.turns || 0));
  parts.push(`<div class="card">
    <h2>用量（14 天）</h2>
    ${ACCT.usage.length ? `<div style="display:flex;align-items:flex-end;gap:3px;height:80px;margin:8px 0 6px">
      ${ACCT.usage.map(r => `<div title="${esc(r.day)} · ${r.turns} 輪"
        style="flex:1;min-width:4px;height:${Math.max(3, (r.turns || 0) / maxTurn * 78)}px;
        background:linear-gradient(180deg,var(--accent),rgba(255,157,77,.25));border-radius:3px 3px 0 0"></div>`).join('')}
      </div>
      <div class="kv" style="font-size:12px">
        ${ACCT.usage.slice(-7).reverse().map(r => `<div>${esc(r.day)}</div>
          <div>${r.turns || 0} 輪 · ${(r.tokens_in || 0) + (r.tokens_out || 0)} tok · ${r.chars_out || 0} 字</div>`).join('')}
      </div>` : '<div class="hint">還沒有用量。</div>'}
  </div>`);

  /* 工作階段 */
  parts.push(`<div class="card">
    <h2>登入中的裝置</h2>
    <div style="margin-bottom:10px">
      ${ACCT.sessions.length ? ACCT.sessions.map(s => `
        <div class="rec" style="padding:9px 12px;margin-bottom:7px">
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            ${s.current ? '<span class="tag ok">這一個</span>' : '<span class="tag">其他</span>'}
            <span style="flex:1"></span>
            <span class="tag">${esc((s.ip || '').slice(0, 24))}</span>
          </div>
          <div style="font-size:11.5px;color:var(--dim);margin-top:4px;word-break:break-all">
            ${esc((s.ua || '').slice(0, 70))} · 最後活動 ${esc(acctFmtTs(s.last))}
          </div>
        </div>`).join('') : '<div class="hint">沒有工作階段。</div>'}
    </div>
    <button class="ghost" id="acctKillSessions" style="width:100%">登出所有裝置</button>
  </div>`);

  /* 帳戶管理（owner） */
  if (ACCT.me.is_owner){
    parts.push(`<div class="card">
      <h2>帳戶管理 <span style="color:var(--dim);font-weight:400;text-transform:none">${ACCT.users.length} 個</span></h2>
      <div class="field"><label>新帳戶名稱</label><input id="acctNewName" placeholder="例如：nono-phone" autocomplete="off"></div>
      <button class="primary" id="acctAddUser" style="width:100%">建立帳戶（自動產生密碼）</button>
      <div id="acctAddOut" style="margin-top:10px"></div>
      <div style="margin-top:12px">
        ${ACCT.users.map(x => `
          <div class="rec" style="padding:10px 12px;margin-bottom:8px">
            <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
              <strong style="font-size:13px">${esc(x.name)}</strong>
              <span class="tag">${esc(x.role)}</span>
              <span class="tag ${x.enabled ? 'ok' : 'bad'}">${x.enabled ? '啟用' : '停用'}</span>
              <span class="tag">${x.keys || 0} 金鑰</span>
              <span style="flex:1"></span>
              ${x.id === u.id ? '<span class="tag">你</span>' : `
                <button class="ghost" data-user="${esc(x.name)}" data-act="reset" style="padding:3px 8px;font-size:11px">重設密碼</button>
                <button class="ghost" data-user="${esc(x.name)}" data-act="${x.enabled ? 'disable' : 'enable'}" style="padding:3px 8px;font-size:11px">${x.enabled ? '停用' : '啟用'}</button>
                <button class="ghost" data-user="${esc(x.name)}" data-act="delete" style="padding:3px 8px;font-size:11px">刪除</button>`}
            </div>
            <div style="font-size:11.5px;color:var(--dim)">
              今日 ${(x.usage_today || {}).turns || 0} 輪 · 上次登入 ${esc(acctFmtTs(x.last_login))}
            </div>
          </div>`).join('')}
      </div>
    </div>`);
  }

  /* 帳戶制度設定（owner） */
  if (ACCT.me.is_owner){
    const S = ACCT.settings || {};
    parts.push(`<div class="card">
      <h2>帳戶制度</h2>
      <div class="hint" style="margin-bottom:12px;line-height:1.7">
        金鑰一律存在這台機器（<code>proxy/keys.json</code>），密碼只存雜湊。
        沒有任何一個位元組會離開這台裝置。
      </div>
      <label style="display:flex;gap:10px;align-items:flex-start;margin-bottom:12px;cursor:pointer">
        <input type="checkbox" id="acctReq" style="margin-top:3px" ${S.require_account ? 'checked' : ''}>
        <span style="font-size:13px">
          <strong>金鑰必須屬於帳戶</strong>
          <div class="hint" style="margin-top:3px">
            開啟後，手動貼進 client_keys.txt 的老金鑰會失效。只有從帳戶產生的金鑰能用。
          </div>
        </span>
      </label>
      <div class="field">
        <label>每個帳戶的金鑰上限（0 = 不限）</label>
        <input id="acctMaxKeys" type="number" min="0" value="${S.max_keys_per_user || 0}">
      </div>
      <button class="primary" id="acctSaveSettings" style="width:100%">儲存</button>
    </div>`);
  }

  box.innerHTML = parts.join('');
  acctWire();
  acctPaintOneShot();
}

/* 一次性明文（金鑰 / 連結 / 新帳戶密碼）。
 *
 * 這三樣都只存在於伺服器的「那一個回應」裡：金鑰與密碼只存雜湊，回看連結的
 * vid 也只回一次。所以它必須被畫在重繪之後、而且要醒目 —— 一個被重繪蓋掉的
 * 明文，使用者唯一的補救是再建一次。 */
function acctPaintOneShot(){
  // 先清掉既有的那一張：acctLoadAll() 也會畫一次（切回分頁時不能消失），
  // 所以「重繪 + 再畫」會變成兩張重複的卡。
  document.querySelectorAll('#acctOneShot').forEach(el => el.remove());
  const o = ACCT.oneShot;
  if (!o || !o.text) return;
  const host = $('#acctBody');
  if (!host) return;
  const accent = o.kind === 'password' ? '255,157,77' : '78,205,196';
  const title = o.kind === 'key' ? 'API 金鑰（只顯示這一次）'
              : o.kind === 'password' ? '新帳戶密碼（只顯示這一次）'
              : '回看連結（可隨時撤銷）';
  const sub = o.label ? esc(o.label) + ' · ' : '';
  const div = document.createElement('div');
  div.id = 'acctOneShot';
  div.className = 'card';
  div.style.cssText = 'background:rgba(' + accent + ',.07);border-color:rgba(' +
    accent + ',.38);order:-1';
  div.innerHTML = `<div style="font-size:12px;color:var(--dim);margin-bottom:6px">
      ${esc(title)}</div>
    <code style="font-size:12.5px;word-break:break-all;display:block;line-height:1.6">
      ${esc(sub + o.text)}</code>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="primary" id="acctOneCopy" style="flex:1">複製</button>
      <button class="ghost" id="acctOneClose" style="flex:1">關閉</button>
    </div>`;
  host.insertBefore(div, host.firstChild);
  const cp = $('#acctOneCopy');
  if (cp) cp.onclick = () => acctCopy(o.text);
  const cl = $('#acctOneClose');
  if (cl) cl.onclick = () => { ACCT.oneShot = null; div.remove(); };
}

function acctWire(){
  const on = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };

  on('#acctSaveSettings', async () => {
    const payload = { settings: {
      require_account: !!($('#acctReq') || {}).checked,
      max_keys_per_user: parseInt(($('#acctMaxKeys') || {}).value || '0', 10) || 0
    } };
    const r = await acctFetch('/a/settings', {
      method: 'POST', body: JSON.stringify(payload) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { toast(d.error || '儲存失敗'); return; }
    ACCT.settings = d.settings || {};
    toast('已儲存');
  });

  on('#acctLogout', async () => {
    await acctFetch('/a/logout', { method: 'POST' });
    acctLoadAll();
  });

  on('#acctPw', () => {
    const f = $('#acctPwForm');
    if (f) f.style.display = f.style.display === 'none' ? '' : 'none';
  });

  on('#acctPwGo', async () => {
    const old = ($('#acctOldPw') || {}).value || '';
    const nw = ($('#acctNewPw') || {}).value || '';
    if (nw.length < 8) { toast('新密碼至少 8 個字元'); return; }
    const r = await acctFetch('/a/password', {
      method: 'POST', body: JSON.stringify({ old, new: nw }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { toast(d.error || '失敗'); return; }
    toast('密碼已更新，請重新登入');
    acctLoadAll();
  });

  on('#acctMint', async () => {
    const label = ($('#acctKeyLabel') || {}).value || '';
    const r = await acctFetch('/a/keys', {
      method: 'POST', body: JSON.stringify({ label }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { toast(d.error || '建立失敗'); return; }
    // ★ 順序很重要：先重繪清單，再把一次性明文貼回畫面上。
    // acctLoadAll() 會整個重建 acctBody，所以「先貼上再重繪」等於把明文擦掉 ——
    // 而明文只存在於這一個回應裡，擦掉就真的沒了（使用者得再建一把）。
    ACCT.oneShot = { kind: 'key', text: d.token || '', label: d.label || label };
    await acctLoadAll();
    acctPaintOneShot();
  });

  on('#acctNewView', async () => {
    const conv = ($('#acctViewConv') || {}).value || '';
    if (!conv) { toast('沒有可分享的對話'); return; }
    const r = await acctFetch('/a/views', {
      method: 'POST',
      body: JSON.stringify({
        conv,
        kind: ($('#acctViewKind') || {}).value || 'chat',
        ttl: parseInt(($('#acctViewTtl') || {}).value || '604800', 10)
      }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { toast(d.error || '產生失敗'); return; }
    ACCT.oneShot = { kind: 'view', text: d.url || '', label: '' };
    await acctLoadAll();
    acctPaintOneShot();
  });

  on('#acctKillSessions', async () => {
    if (!await ask('登出所有裝置（包括這一個）？')) return;
    await acctFetch('/a/sessions', { method: 'DELETE' });
    toast('已登出所有裝置');
    acctLoadAll();
  });

  on('#acctAddUser', async () => {
    const name = ($('#acctNewName') || {}).value || '';
    if (!name.trim()) { toast('請填名稱'); return; }
    const r = await acctFetch('/a/users', {
      method: 'POST', body: JSON.stringify({ action: 'create', name: name.trim() }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { toast(d.error || '建立失敗'); return; }
    ACCT.oneShot = { kind: 'password', text: d.password || '',
                     label: (d.user || {}).name || name };
    await acctLoadAll();
    acctPaintOneShot();
  });

  document.querySelectorAll('[data-revoke]').forEach(el => el.onclick = async () => {
    if (!await ask('撤銷這把金鑰？用它的裝置會立刻失去存取。')) return;
    await acctFetch('/a/keys', {
      method: 'DELETE', body: JSON.stringify({ id: el.dataset.revoke }) });
    await acctLoadAll();
  });

  document.querySelectorAll('[data-rmview]').forEach(el => el.onclick = async () => {
    if (!await ask('撤銷這個回看連結？對方會立刻打不開。')) return;
    await acctFetch('/a/views', {
      method: 'DELETE', body: JSON.stringify({ vid: el.dataset.rmview }) });
    await acctLoadAll();
  });

  document.querySelectorAll('[data-copy]').forEach(el => el.onclick = () => {
    acctCopy(el.dataset.copy);
  });

  document.querySelectorAll('[data-user]').forEach(el => el.onclick = async () => {
    const name = el.dataset.user, act = el.dataset.act;
    if (act === 'delete' && !await ask('刪除帳戶 ' + name + '？它的金鑰與連結都會失效。')) return;
    const r = await acctFetch('/a/users', {
      method: 'POST', body: JSON.stringify({ action: act, name }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { toast(d.error || '失敗'); return; }
    if (act === 'reset') {
      toast('新密碼：' + (d.password || ''));
    } else {
      toast('已' + ({ enable: '啟用', disable: '停用', delete: '刪除' }[act] || act));
    }
    await acctLoadAll();
  });
}

function acctCopy(text){
  if (!text) return;
  try {
    navigator.clipboard.writeText(text).then(() => toast('已複製'),
      () => toast('複製失敗，請長按選取'));
  } catch (e) {
    toast('複製失敗，請長按選取');
  }
}

/* 從「記錄」頁一鍵產生回看連結（owner 或本人）。 */
async function acctShareConv(conv, kind){
  if (!ACCT.me){ toast('請先登入'); show('account'); return; }
  const r = await acctFetch('/a/views', {
    method: 'POST', body: JSON.stringify({ conv, kind: kind || 'chat',
                                           ttl: 604800 }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.ok) { toast(d.error || '產生失敗'); return; }
  await acctCopy(d.url || '');
  toast('回看連結已複製');
}

/* 啟動：先掛分頁，再問身分。 */
(function acctBoot(){
  const tabs = $('#tabs');
  if (tabs && !tabs.querySelector('[data-view="account"]')){
    const b = document.createElement('button');
    b.dataset.view = 'account';
    b.textContent = '帳戶';
    b.dataset.i18n = 'tab.account';
    tabs.appendChild(b);
    b.onclick = () => show('account');
    if (window.I18N && typeof window.I18N.applyDOM === 'function') {
      window.I18N.applyDOM();
    }
  }
  const main = document.querySelector('main');
  if (main && !$('#view-account')){
    const v = document.createElement('div');
    v.className = 'view';
    v.id = 'view-account';
    v.innerHTML = '<div id="acctBody"></div>';
    main.appendChild(v);
  }
  setTimeout(acctLoadAll, 80);
})();
