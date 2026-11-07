/* Keys + connection mode.
 *
 * Split out from app.js so the credential path stays readable — a bug here is
 * "everything is open" or "nothing works", not a cosmetic defect.
 *
 * Two rules this file exists to enforce:
 *   1. the raw token is displayed exactly once, at creation, and never fetched
 *      again (the server only ever returns a prefix)
 *   2. switching to 本機 mode is a deliberate act with a stated consequence, not
 *      a silent toggle: it removes the key requirement for every caller that
 *      reaches the port.
 */

// lastToken: plaintext of a key created in this page session, so the
// "複製位址 + 金鑰" button can include it. Never persisted anywhere.
const KEYS = { list: [], access: null, mode: '', lastToken: '' };
const _tr = (k, fb) => (window.I18N && window.I18N.t ? window.I18N.t(k, fb) : fb);

/* Server root for this page's admin calls.
 *
 * Always the origin that served the page. The console is served BY the proxy,
 * so `location.origin` IS the proxy — and `cfg.base` is what a CLIENT should
 * connect to, which is routinely mistyped as the upstream URL. Following
 * cfg.base here sent admin calls to OpenAI and produced HTML error pages that
 * read as "format errors" (measured 2026-11-05).
 *
 * Defined in this file because keys.js is the only module that calls `root()`;
 * app.js carries AUTH()/authHdr() and log.js carries traceRoot().
 */
function root(){
  try {
    if (location.protocol === 'http:' || location.protocol === 'https:') return location.origin;
  } catch (e) { /* fall through */ }
  const typed = (cfg.base || '').replace(/\/+$/, '').replace(/\/v1$/, '');
  return typed || '';
}

/* ---------- connection mode ---------- */
function paintMode(){
  // Read-only here. The mode is chosen once, in the setup wizard, together with
  // the models and the key it applies to. Keeping a second toggle on this page
  // meant the same setting had two controls that could disagree, and flipping it
  // here did not re-issue or revoke anything — only the wizard does the whole
  // operation coherently.
  const v = $('#modeValue');
  if (v) {
    v.innerHTML = KEYS.mode === 'local'
      ? `<span style="color:var(--warn)">${_tr('keys.mode_local', '對內（免金鑰）')}</span>`
      : `<span style="color:var(--ok)">${_tr('keys.mode_remote', '對外（需金鑰）')}</span>`;
  }
  const h = $('#modeHint');
  if (!h) return;
  h.textContent = KEYS.mode === 'local'
    ? _tr('keys.hint_local', '任何能連到這個 port 的人都能直接使用，不需要金鑰。只適合純本機。')
    : _tr('keys.hint_remote', '任何連到這個網址的請求都必須帶金鑰。開過隧道就該用這個。');
}

window.addEventListener('forge:langchange', () => paintMode());

$('#btnGoWizard').onclick = () => show('setup');

async function loadAccess(){
  const box = $('#accessKv');
  if (!root()) { box.innerHTML = '<div>—</div><div style="color:var(--dim)">讀不到伺服器位址</div>'; return; }
  try {
    const r = await adminFetch('/access');
    if (!r.ok) throw new Error('HTTP ' + r.status + (r.status === 401 ? '：金鑰無效' : ''));
    const d = await readJson(r);
    if (!d.ok) throw new Error((d.error && d.error.message) || 'failed');
    KEYS.access = d;
    if (d.model_map) paintModels(d.model_map);
    if (!KEYS.mode) KEYS.mode = d.require_key ? 'remote' : 'local';
    paintMode();
    const publicUrl = d.public_base_url || '';
    // The thing an operator actually pastes into a client is the API base URL
    // AND a key. Showing only the URL meant they had to scroll to another card,
    // create a key, and then assemble the two by hand. Offer the ready-to-use
    // pair, with the key masked until the operator asks for it.
    const u = publicUrl || d.base_url || '';
    $('#accessPrimary').innerHTML =
      '<span>' + esc(publicUrl ? '公網 API' : '本機 API') + '</span>' +
      '<code>' + esc(u || '尚無網址') + '</code>' +
      (u ? '<div style="display:flex;gap:8px;margin-top:8px">' +
             '<button class="mini" id="btnCopyEndpoint">複製位址 + 金鑰</button>' +
             (publicUrl ? '<button class="mini" id="btnCopyPublic">只複製位址</button>' : '') +
           '</div>' +
           '<div class="hint" id="endpointHint" style="margin-top:8px;font-size:11px"></div>' : '');
    const ce = $('#btnCopyEndpoint');
    if (ce) ce.onclick = () => copyEndpoint(u);
    const cp = $('#btnCopyPublic');
    if (cp) cp.onclick = async () => {
      try { await navigator.clipboard.writeText(u); toast('已複製位址'); }
      catch { toast('複製失敗，長按選取'); }
    };
    box.innerHTML = [
      ['本機 API', d.base_url],
      ['公網 API', publicUrl || (d.behind_tunnel ? d.base_url : '尚無已驗證的隧道網址')],
      ['目前入口', d.origin],
      ['隧道', d.tunnel_url ? '已驗證（' + (d.tunnel_checked_ago_s || 0) + 's 前）'
              : d.tunnel_note ? '失效：' + d.tunnel_note
              : d.behind_tunnel ? '有（經由轉發）' : '無（直接連線）'],
      ['需要金鑰', d.require_key ? '是' : '否'],
      ['可用金鑰', d.enabled_keys + ' / ' + d.total_keys + ' 把'],
      ['模型', (d.models || []).map(a => {
        const r = (d.model_map || {})[a];
        return r && r !== a ? a + ' → ' + r : a;
      }).join('\n')],
    ].map(([k, v]) => '<div>' + esc(k) + '</div><div style="white-space:pre-wrap">' +
         esc(String(v)) + '</div>').join('');
    fillKeyList();
  } catch (e) {
    box.innerHTML = '<div>—</div><div style="color:var(--bad)">' + esc(e.message || String(e)) + '</div>';
  }
}

/* Copy endpoint + key as one pasteable block.
 *
 * WHY: the whole point of this page is "here is what to paste into your client",
 * and that has always needed TWO values (base URL and a token). Offering them
 * separately is how an operator ends up pasting a URL with no key and reading
 * the resulting 401 as "the proxy is broken".
 *
 * A key's plaintext only exists at creation time (the store keeps a hash), so
 * this uses: the key just created in this page session, else the one saved in
 * 設定, else it explains that a new one must be created. It never invents one,
 * and it never prints a key that was not already visible to this browser.
 */
async function copyEndpoint(url){
  const hint = document.querySelector('#endpointHint');
  const say = (t, bad) => {
    if (hint) { hint.textContent = t; hint.style.color = bad ? 'var(--bad)' : 'var(--dim)'; }
    toast(t);
  };
  const key = (KEYS.lastToken || cfg.key || '').trim();
  if (!key) {
    say('還沒有金鑰可一起複製；先按下方「建立新的金鑰」，或到設定填一把。', true);
    return;
  }
  const block = 'Base URL: ' + url + '\nAPI Key:  ' + key;
  try {
    await navigator.clipboard.writeText(block);
    const masked = key.slice(0, 10) + '…' + key.slice(-4);
    say('已複製位址與金鑰（' + masked + '）');
  } catch (e) {
    say('複製失敗，請長按選取：' + netErr(e), true);
  }
}

$('#btnCopyUrl').onclick = async () => {
  const a = KEYS.access || {};
  const pub = a.public_base_url || '';
  // Prefer the verified public URL. When it is stale, copying it is handing out
  // a dead hostname — say why instead, and offer the local one explicitly.
  const u = pub || a.base_url || cfg.base || '';
  if (!u) { toast('還沒有網址'); return; }
  if (!pub && a.tunnel_ok === false) {
    toast('公網網址已失效，已改複製本機網址');
  }
  try { await navigator.clipboard.writeText(u); toast('已複製：' + u); }
  catch { toast('複製失敗，長按選取'); }
};
$('#btnAccessReload').onclick = async () => {
  await Promise.all([loadAccess(), loadKeys()]);
};
$('#btnTunnelRestart').onclick = async () => {
  const hint = $('#tunnelHint');
  const btn = $('#btnTunnelRestart');
  if (!await ask('重生公網網址？\n舊網址會立刻失效，任何貼著舊網址的裝置都要換新的。\n（只在本機開啟時可用）')) return;
  btn.disabled = true; hint.textContent = '重生中…（最多 60 秒）';
  try {
    const r = await adminFetch('/tunnel/restart', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error((d.error && d.error.message) || d.note || ('HTTP ' + r.status));
    hint.innerHTML = '✓ 新網址：<code>' + esc(d.app_url || d.url) + '</code>';
    await Promise.all([loadAccess(), loadKeys()]);
    toast('已重生公網網址');
  } catch (e) {
    hint.innerHTML = '<span style="color:var(--bad)">重生失敗：' + esc(netErr(e)) + '</span>';
  } finally {
    btn.disabled = false;
  }
};

/* ---------- keys ---------- */
async function loadKeys(){
  if (!root()) return;
  try {
    const r = await adminFetch('/keys');
    if (r.status === 401) {
      $('#keyList').innerHTML =
        '<div style="color:var(--warn);font-size:12px">需要金鑰才能管理金鑰。先到「設定」填一把。</div>';
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await readJson(r);
    KEYS.list = d.keys || [];
    if (KEYS.access) {
      KEYS.access.enabled_keys = KEYS.list.filter(k => k.enabled).length;
      KEYS.access.total_keys = KEYS.list.length;
      const rows = $('#accessKv').querySelectorAll('div');
      for (let i = 0; i < rows.length; i += 2) {
        if (rows[i].textContent === '可用金鑰') {
          rows[i + 1].textContent = KEYS.access.enabled_keys + ' / ' + KEYS.access.total_keys + ' 把';
          break;
        }
      }
    }
    if (!KEYS.mode) KEYS.mode = d.require_key ? 'remote' : 'local';
    paintMode();
    fillKeyList();
  } catch (e) {
    $('#keyList').innerHTML = '<div style="color:var(--bad);font-size:12px">' +
      esc(netErr(e)) + '</div>';
  }
}

function fillKeyList(){
  const box = $('#keyList');
  const n = KEYS.list.filter(k => k.enabled).length;
  $('#keyCount').textContent = '· ' + n + ' 把啟用中 / 共 ' + KEYS.list.length + ' 把';
  if (!KEYS.list.length) {
    box.innerHTML = '<div style="color:var(--dim);font-size:12px">還沒有金鑰。' +
      '建立一把，貼到上面的 API Key 欄位。</div>';
    return;
  }
  box.innerHTML = KEYS.list.map(k => `
    <div class="row" style="margin-bottom:8px;flex-wrap:wrap;gap:6px">
      <label style="min-width:0;flex:1">
        <b style="color:var(--fg)">${esc(k.label || '(無標籤)')}</b><br>
        <span style="font-family:monospace;font-size:11px">${esc(k.prefix)}</span><br>
        <span style="font-size:10.5px;opacity:.7">
          建立 ${esc(k.created_when || '—')}
          ${k.last_used_when ? ' · 最後使用 ' + esc(k.last_used_when) : ''}
          ${k.hits ? ' · ' + k.hits + ' 次' : ''}
        </span>
        ${k.uid
          ? (k.account ? '<div style="font-size:10.5px;color:var(--dim)">帳戶 · ' +
              esc(k.account) + '</div>' : '')
          : '<div style="font-size:10.5px;color:var(--warn)">' +
            '⚠ 沒有帳戶歸屬（手貼或舊版建立）。到「帳戶」頁建立，才看得到是誰在用。</div>'}
      </label>
      <span style="display:flex;gap:6px;align-items:center">
        <span class="tag ${k.enabled ? 'ok' : 'bad'}">${k.enabled ? '啟用' : '停用'}</span>
        <button class="mini" data-tog="${esc(k.id)}" data-on="${k.enabled ? '0' : '1'}">
          ${k.enabled ? '停用' : '啟用'}</button>
        <button class="mini" data-del="${esc(k.id)}" style="color:var(--bad)">刪除</button>
      </span>
    </div>`).join('');

  box.querySelectorAll('button[data-tog]').forEach(b => b.onclick = async () => {
    await keyAction(b.dataset.on === '1' ? 'enable' : 'disable', b.dataset.tog);
  });
  box.querySelectorAll('button[data-del]').forEach(b => b.onclick = async () => {
    if (!await ask('刪除這把金鑰？使用它的裝置會立刻失效，且無法復原。',
                   {ok:'刪除', danger:true})) return;
    await keyAction('delete', b.dataset.del);
  });
}

async function keyAction(action, id){
  try {
    const r = await adminFetch('/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action, id })
    });
    const d = await readJson(r);
    if (!d.ok) throw new Error((d.error && d.error.message) || 'failed');
    await loadKeys();
    toast(action === 'delete' ? '已刪除' : action === 'enable' ? '已啟用' : '已停用');
  } catch (e) {
    toast('失敗：' + netErr(e));
  }
}

$('#btnNewKey').onclick = async () => {
  const label = ($('#newKeyLabel').value || '').trim();
  $('#newKeyOut').innerHTML = '<div style="color:var(--dim);font-size:12px">建立中…</div>';
  try {
    const r = await adminFetch('/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action: 'create', label })
    });
    const d = await readJson(r);
    if (!d.ok) throw new Error((d.error && d.error.message) || 'failed');
    const tok = d.key.token;
    // Remember it for this page's lifetime so 複製位址 + 金鑰 can include it.
    // Plaintext exists only here (the store holds a hash) and is never persisted.
    KEYS.lastToken = tok;
    $('#newKeyOut').innerHTML =
      '<div style="border:1px solid var(--ok);border-radius:10px;padding:12px;' +
      'background:#0f1a13">' +
      '<div style="color:var(--ok);font-weight:600;margin-bottom:8px">' +
      '金鑰只會顯示這一次</div>' +
      '<pre id="newTok" style="margin:0;font-size:12.5px;word-break:break-all;' +
      'white-space:pre-wrap;color:#dff5e5">' + esc(tok) + '</pre>' +
      '<div style="display:flex;gap:8px;margin-top:10px">' +
      '<button class="mini" id="btnCopyTok">複製</button>' +
      '<button class="mini" id="btnUseTok">直接用到設定</button></div>' +
      '<div style="font-size:11px;opacity:.7;margin-top:8px">' +
      '離開這頁就看不到了。伺服器只存雜湊，沒有辦法再印出來。</div></div>';
    $('#btnCopyTok').onclick = async () => {
      try { await navigator.clipboard.writeText(tok); toast('已複製'); }
      catch { toast('複製失敗，長按選取'); }
    };
    $('#btnUseTok').onclick = () => {
      cfg.key = tok; save();
      $('#fKey').value = tok;
      toast('已填入設定');
    };
    $('#newKeyLabel').value = '';
    await loadKeys();
  } catch (e) {
    $('#newKeyOut').innerHTML = '<div style="color:var(--bad);font-size:12px">' +
      esc(e.message || String(e)) + '</div>';
  }
};

/* Refresh activity timestamps while visible; avoid background polling. */
let keyRefreshTimer = 0;
document.addEventListener('visibilitychange', () => {
  clearInterval(keyRefreshTimer);
  keyRefreshTimer = document.hidden ? 0 : setInterval(() => {
    if (document.querySelector('#view-keys.on')) loadKeys();
  }, 15000);
});

/* Refresh "last used" while the page is visible.
 *
 * The question this answers is "is this key actually in use" — the first thing
 * anyone asks when a key may have leaked, and a value that only updates on
 * reload makes the operator reload to find out. Polling stops when the tab is
 * hidden so a backgrounded app does not keep hitting the proxy. */
let _keyTimer = null;
function startKeyPolling(){
  stopKeyPolling();
  _keyTimer = setInterval(() => {
    if (document.hidden) return;
    if (!document.getElementById('view-keys').classList.contains('on')) return;
    loadKeys();
  }, 15000);
}
function stopKeyPolling(){
  if (_keyTimer) { clearInterval(_keyTimer); _keyTimer = null; }
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && document.getElementById('view-keys').classList.contains('on')) {
    loadKeys();
  }
});

/* Load on tab open, and after the connection settings change. */
const _showKeys = window.show;
window.show = function(v){
  _showKeys(v);
  if (v === 'keys') { loadAccess(); loadKeys(); startKeyPolling(); }
  else { stopKeyPolling(); }
};
