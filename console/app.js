/* POPO-V2 Console — simplified single-page setup. */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const LS = 'popo_v2_console';

// Language control. Kept here rather than in i18n.js because it is a DOM
// concern: i18n.js must stay runnable outside a browser for the dictionary test.
document.addEventListener('DOMContentLoaded', function () {
  const picker = $('#langSelect');
  if (!picker) return;
  picker.value = currentLang;
  picker.onchange = () => setLang(picker.value);
});

let state = {
  base: '',
  upstreamHost: '',
  upstreamKey: '',
  upstreamPrefix: '',
  key: '',
  keyScope: 'public',
  configured: false,
  published: [],
  available: [],
  sourceModels: [],
};
let sourceRowsCache = [];
const ACCESS_VIEW = { apiBase: '', public: false, publicBase: '', internalBase: '' };

function clientBaseForScope(scope) {
  return scope === 'public' ? ACCESS_VIEW.publicBase : ACCESS_VIEW.internalBase;
}

function loadState() {
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(LS) || '{}')); } catch {}
  // Credentials belong to the current tab only. Older releases persisted both
  // upstream and client keys in localStorage, so remove them during migration.
  delete state.upstreamKey;
  delete state.key;
  delete state.adminKey;
  // A stored selection is only meaningful against the catalogue it came from.
  // Clearing it here keeps the counter honest before any request is made.
  const known = new Set(state.available || []);
  state.published = (state.published || []).filter(m => known.has(m));
  for (const m of Object.keys(state.armor || {})) {
    if (!known.has(m)) delete state.armor[m];
  }
  saveState();
}
function saveState() {
  const safe = { ...state };
  delete safe.upstreamKey;
  delete safe.key;
  delete safe.adminKey;
  localStorage.setItem(LS, JSON.stringify(safe));
}

function readBody(r) {
  const ct = r.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) return r.json().catch(() => ({}));
  return r.text().then(t => ({ raw: t }));
}

async function api(method, path, body) {
  const url = state.base + path;
  const headers = { 'Accept': 'application/json' };
  const adminKey = ($('#adminKey')?.value || '').trim();
  if (adminKey) headers['Authorization'] = 'Bearer ' + adminKey;
  else if (state.key) headers['Authorization'] = 'Bearer ' + state.key;
  if (body) headers['Content-Type'] = 'application/json';
  try {
    const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined,
      credentials: 'same-origin' });
    const data = await readBody(res);
    return { ok: res.ok, status: res.status, data };
  } catch (error) {
    return { ok: false, status: 0, data: { error: { message: error?.message || 'Network request failed' } } };
  }
}

function normalizeBase(v) {
  let raw = String(v || '').trim().replace(/\/+$/, '');
  if (!raw) return '';
  if (!raw.startsWith('http')) raw = 'http://' + raw;
  try {
    const u = new URL(raw);
    return u.protocol + '//' + u.host + u.pathname.replace(/\/+$/, '');
  } catch { return raw.replace(/\/+$/, ''); }
}

function showMsg(text, ok) {
  const el = $('#msg');
  el.textContent = text || '';
  el.style.display = 'block';
  el.className = ok ? 'ok' : 'err';
}
function clearMsg() {
  const el = $('#msg');
  el.style.display = 'none';
  el.textContent = '';
  el.className = '';
}

function showTab(name) {
  // Result and armor were separate V1-style tabs; keep their deep links mapped
  // into the simpler setup and tuning views.
  if (name === 'result' || name === 'armor') name = name === 'result' ? 'setup' : 'tuning';
  $$('.step').forEach(s => s.classList.toggle('on',
    s.id === 'tab-' + name || (name === 'sources' && s.id === 'panel-sources')));
  $$('.tabs button').forEach(b => {
    const active = b.dataset.tab === name || (name === 'sources' && b.dataset.tab === 'manage');
    b.classList.toggle('on', active);
    b.setAttribute('aria-selected', String(active));
  });
  if (name === 'sources') loadSources();
  if (name === 'manage') { loadKeys(); loadAccounts(); loadAccess(); }
  if (name === 'records') loadTraceClients();
  if (name === 'tuning') loadTuning();
}

function renderModels() {
  const list = $('#modelList');
  if (!state.available.length) {
    $('#modelGroup').style.display = 'none';
    // The counter would otherwise keep a stale number from a previous session.
    if (state.published.length) state.published = [];
    updateSelectedCount();
    return;
  }
  $('#modelGroup').style.display = 'block';
  list.replaceChildren();

  // Armour choices come from the server, not from a list written here: the registry
  // is the source of truth and an operator may edit it without touching this file.
  const armorFamilies = (state.armorFamilies && state.armorFamilies.length)
    ? state.armorFamilies : [];
  const armorNames = armorFamilies.map(f => typeof f === 'string' ? f : f.id);
  const armorLabel = (name) => {
    const d = (state.armorDetails || {})[name];
    const family = armorFamilies.find(f => (typeof f === 'string' ? f : f.id) === name);
    return (family && typeof family === 'object' && family.label) || (d && d.label) || name;
  };

  state.available.forEach((m, i) => {
    const row = document.createElement('div');
    row.className = 'model-item';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'm-' + i;
    cb.value = m;
    cb.checked = state.published.includes(m);

    const label = document.createElement('label');
    label.htmlFor = cb.id;
    label.textContent = m;
    label.style.flex = '1';
    label.style.minWidth = '0';
    label.style.overflow = 'hidden';
    label.style.textOverflow = 'ellipsis';

    // Per-model armour. "default" means "let the model decide from its family",
    // which is what almost every model should stay on.
    const select = document.createElement('select');
    select.className = 'armor-select';
    select.dataset.model = m;
    [''].concat(armorNames).forEach(name => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name ? armorLabel(name) : '自動（依模型指紋）';
      if (name) {
        const family = armorFamilies.find(f => (typeof f === 'string' ? f : f.id) === name);
        const detail = (family && family.desc) || (state.armorDetails[name] || {}).desc;
        if (detail) opt.title = detail;
      }
      select.appendChild(opt);
    });
    const current = (state.armor && state.armor[m]) || '';
    select.value = current;
    select.onchange = () => {
      state.armor = state.armor || {};
      if (select.value) state.armor[m] = select.value;
      else delete state.armor[m];
      saveState();
    };
    // Deliberately NOT disabled when unchecked: choosing armour and choosing to
    // publish are separate decisions, and locking the dropdown until the checkbox
    // is ticked makes the natural order ("pick the armour I want") impossible.
    // Dimmed instead, so it still reads as inactive-but-usable.
    select.style.opacity = cb.checked ? '1' : '.72';

    cb.onchange = () => {
      select.style.opacity = cb.checked ? '1' : '.72';
      state.published = Array.from(list.querySelectorAll('input[type=checkbox]:checked')).map(i => i.value);
      saveState();
      updateSelectedCount();
    };

    row.append(cb, label, select);
    list.appendChild(row);
  });
  updateSelectedCount();
}

function updateSelectedCount() {
  const el = $('#modelCount');
  if (!el) return;
  // Count what is actually ticked, not what the state object remembers. The state
  // can hold a selection from a previous catalogue, and a counter that disagrees
  // with the visible checkboxes is worse than no counter.
  const list = $('#modelList');
  const ticked = list ? list.querySelectorAll('input[type=checkbox]:checked').length : 0;
  el.textContent = ticked ? `${ticked} 個已選` : '';
  // Keep state in step with the DOM so a save writes what the operator sees.
  state.published = list
    ? Array.from(list.querySelectorAll('input[type=checkbox]:checked')).map(i => i.value)
    : [];
  const setup = $('#btnSetup');
  if (setup) setup.disabled = ticked === 0;
}

async function loadArmorFamilies() {
  const { ok, data } = await api('GET', '/armor/registry');
  if (!ok || !data) return;
  // `families` is the 2.5.0 field; fall back to `profiles` for an older server.
  state.armorFamilies = data.families || data.profiles || [];
  state.armorDetails = data.details || {};
  renderModels();
}

function fillForm() {
  $('#upstreamHost').value = state.upstreamHost || '';
  $('#upstreamKey').value = state.upstreamKey || '';
  $('#upstreamPrefix').value = state.upstreamPrefix || '';
  if (state.configured || state.key) {
    const keyScope = state.keyScope || 'public';
    $('#outUrl').textContent = clientBaseForScope(keyScope) || '尚無可用的此範圍網址';
    $('#outUrlHint').textContent = keyScope === 'public'
      ? (ACCESS_VIEW.public ? '對外金鑰；公開網址已驗證，可跨網路使用。' : '對外網址尚未驗證；請先啟動公開隧道。')
      : '對內金鑰；只允許本機或內網呼叫。';
    $('#outKey').textContent = state.key || '金鑰只在建立時顯示；可到「管理」建立新金鑰';
    $('#outModels').textContent = state.published.join(', ') || '-';
    $('#btnCopyClientConfig').disabled = !state.key || !clientBaseForScope(keyScope)
      || (keyScope === 'public' && !ACCESS_VIEW.public);
    $('#btnCopyClientConfig').title = keyScope === 'public' && !ACCESS_VIEW.public
      ? '尚無已驗證的公開網址'
      : (!state.key ? '目前沒有一次性顯示的客戶端金鑰' : '');
    $('#tab-result').classList.add('open');
  } else {
    $('#tab-result').classList.remove('open');
  }
  renderModels();
}

async function refreshStatus() {
  const result = await api('GET', '/setup/status');
  const { ok, data } = result;
  const pill = $('#connectionStatus');
  pill.classList.toggle('offline', !ok && result.status === 0);
  pill.querySelector('.status-text').textContent = result.status === 0 ? '無法連線' : (ok ? '已連線' : '服務可達');
  $('#aboutStatus').textContent = ok
    ? (data.configured ? '服務在線 · 已完成初始設定' : '服務在線 · 尚未完成初始設定')
    : (result.status === 0 ? '無法連線到本機 POPO 服務。' : `服務在線，狀態檢查回應 HTTP ${result.status}。`);
  if (ok && data.configured) {
    state.configured = true;
    state.base = normalizeBase(location.origin);
    state.upstreamHost = data.upstream_host || state.upstreamHost;
    state.available = data.models || [];
    state.published = data.published || [];
    state.armor = data.profiles || state.armor || {};
    $('#btnSetup').textContent = '套用模型與積木';
    fillForm();
    await loadAccess();
    saveState();
    renderModels();
  } else if (ok) {
    state.configured = false;
    $('#btnSetup').textContent = '驗證上游並建立 API';
  }
}

async function loadModelList() {
  clearMsg();
  const host = normalizeBase($('#upstreamHost').value);
  if (!host) return showMsg('請先填寫 Base URL', false);
  const key = $('#upstreamKey').value.trim();
  if (!key) return showMsg('請填寫上游 API Key', false);
  showMsg('連線並讀取模型清單...', true);
  const { ok, data } = await api('POST', '/setup/list-models', {
    upstream_host: host,
    upstream_key: key,
    upstream_path_prefix: $('#upstreamPrefix').value.trim(),
    setup_token: $('#setupToken').value.trim(),
  });
  if (!ok) {
    const detail = data?.error?.detail || data?.error?.message || JSON.stringify(data);
    return showMsg('讀取失敗：' + detail, false);
  }
  state.upstreamHost = host;
  state.upstreamKey = key;
  state.upstreamPrefix = $('#upstreamPrefix').value.trim();
  state.available = data.models || [];
  // Keep a previous selection only if those models still exist in this catalogue.
  // Otherwise the counter says "2 selected" while no checkbox is ticked — the
  // selection lives in localStorage and outlives the list it referred to.
  const availableSet = new Set(state.available);
  state.published = (state.published || []).filter(m => availableSet.has(m));
  state.armor = state.armor || {};
  // Armour choices for models that are gone are dropped for the same reason.
  for (const m of Object.keys(state.armor)) {
    if (!availableSet.has(m)) delete state.armor[m];
  }
  saveState();
  renderModels();
  showMsg(`讀到 ${state.available.length} 個模型。勾選要發布的，並選擇每個模型的積木。`, true);
}

async function doSetup() {
  clearMsg();
  const host = normalizeBase($('#upstreamHost').value);
  const key = $('#upstreamKey').value.trim();
  const published = Array.from($$('#modelList input:checked')).map(i => i.value);
  if (!published.length) return showMsg('請先讀取模型並勾選至少一個模型', false);
  const profiles = {};
  for (const m of published) {
    const chosen = (state.armor || {})[m];
    if (chosen) profiles[m] = chosen;
  }
  $('#btnSetup').disabled = true;
  if (!host || !key) {
    if (!state.configured) {
      $('#btnSetup').disabled = false;
      return showMsg('首次設定請填 Base URL 與上游 API Key', false);
    }
    showMsg('儲存模型與積木設定...', true);
    const updated = await api('POST', '/setup/models', { models: published, profiles });
    $('#btnSetup').disabled = false;
    if (!updated.ok) return showMsg('套用失敗：' + (updated.data?.error?.message || '請確認管理權限'), false);
    state.published = updated.data.published || published;
    state.armor = updated.data.profiles || profiles;
    saveState(); fillForm();
    $('#outModels').textContent = state.published.join(', ') || '-';
    showMsg('模型與積木設定已更新', true);
    return;
  }
  showMsg('連線驗證並套用設定...', true);
  const { ok, data } = await api('POST', '/setup/quick', {
    upstream_host: host,
    upstream_key: key,
    upstream_path_prefix: $('#upstreamPrefix').value.trim(),
    models: published,
    profiles,
    key_scope: $('#setupKeyScope').value,
    setup_token: $('#setupToken').value.trim(),
  });
  $('#btnSetup').disabled = false;
  if (!ok) {
    const detail = data?.error?.detail || data?.error?.message || JSON.stringify(data);
    return showMsg('失敗：' + detail, false);
  }
  state.base = normalizeBase(location.origin);
  state.key = data.key || state.key || '';
  state.keyScope = data.allow_public ? 'public' : 'internal';
  state.keyId = data.key_id || state.keyId || '';
  state.published = data.published || [];
  state.available = data.models || [];
  state.armor = data.profiles || profiles;
  state.configured = true;
  state.upstreamHost = host;
  state.upstreamKey = key;
  await loadAccess();
  saveState();
  fillForm();
  $('#tab-result').classList.add('open');
  showTab('setup');
  $('#tab-result').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  showMsg(data.key ? '設定已套用，API 金鑰已建立' : '設定已更新，沿用現有 API 金鑰', true);
}

function copyTo(id) {
  const el = $('#' + id);
  navigator.clipboard.writeText(el.textContent).then(() => {
    showMsg('已複製', true);
  }).catch(() => showMsg('複製失敗', false));
}

function bind() {
  loadState();
  state.base = normalizeBase(location.origin);
  fillForm();
  refreshStatus();

  $('#tabs').onclick = e => {
    const button = e.target.closest('button[data-tab]');
    if (button) showTab(button.dataset.tab);
  };
  const listBtn = $('#btnList');
  if (listBtn) listBtn.onclick = loadModelList;
  $('#btnSetup').onclick = doSetup;
  $('#btnCopyClientConfig').onclick = () => {
    if (!state.key) return showMsg('此頁沒有可複製的金鑰；請到「管理」建立新客戶端金鑰。', false);
    const scope = state.keyScope || 'public';
    const base = clientBaseForScope(scope);
    if (!base || (scope === 'public' && !ACCESS_VIEW.public)) return showMsg('公開網址目前沒有通過連線檢查。', false);
    copyText(`Base URL: ${base}\nAPI Key: ${state.key}`, scope === 'public' ? '已複製公開網址與客戶端金鑰' : '已複製內網網址與客戶端金鑰');
  };
  $$('.copy').forEach(b => b.onclick = () => copyTo(b.dataset.target));
  $('#btnOpenSources').onclick = () => showTab('sources');
  $('#btnSourcesBack').onclick = () => showTab('manage');
}

bind();

function pretty(target, value) { $(target).textContent = JSON.stringify(value, null, 2); }
function adminReady(result) {
  if (!result.ok) {
    const message = result.data?.error?.detail || result.data?.error?.message || `HTTP ${result.status}`;
    showMsg(message, false); return false;
  }
  return true;
}
function selectedSource() { return $('#sourceSelect').value || $('#sourceId').value.trim(); }

async function loadSources() {
  const result = await api('GET', '/admin/sources');
  if (!adminReady(result)) return;
  sourceRowsCache = result.data.sources || [];
  pretty('#sourceList', sourceRowsCache.map(({id,name,base_url,configured,published_models}) => ({id,name,base_url,configured,published_models})));
  const select = $('#sourceSelect'); select.replaceChildren();
  sourceRowsCache.forEach(row => { const opt=document.createElement('option'); opt.value=row.id; opt.textContent=`${row.name} (${row.id})`; select.appendChild(opt); });
  if (sourceRowsCache.length && !$('#sourceId').value) fillSource(sourceRowsCache[0].id);
  await loadRoutes();
}
function fillSource(id) {
  const row = sourceRowsCache.find(r => r.id === id); if (!row) return;
  $('#sourceSelect').value = row.id; $('#sourceId').value = row.id;
  $('#sourceName').value = row.name; $('#sourceUrl').value = row.base_url;
  $('#sourceSecret').value = '';
}
async function sourceAction(action, extra={}) {
  let path = '/admin/sources';
  let body;
  const sourceId = selectedSource();
  if (action === 'save') {
    body = { action: 'add', id: $('#sourceId').value.trim(),
      name: $('#sourceName').value.trim(), base_url: $('#sourceUrl').value.trim() };
    const secret = $('#sourceSecret').value.trim();
    if (secret) body.keys = [secret];
  } else if (action === 'delete') {
    body = { action: 'remove', id: sourceId };
  } else if (action === 'discover' || action === 'test') {
    path += '/' + action;
    body = { source_id: sourceId, ...extra };
  } else {
    body = { action, source_id: sourceId, ...extra };
  }
  const result = await api('POST', path, body);
  if (!adminReady(result)) return null;
  showMsg(`${action} 完成`, true); return result.data;
}
function renderSourceModels(models) {
  state.sourceModels = models || [];
  const host = $('#sourceModelList'); host.replaceChildren();
  state.sourceModels.forEach((model,i) => {
    const row=document.createElement('div'); row.className='model-item';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.value=model; cb.id='sm-'+i;
    const label=document.createElement('label'); label.htmlFor=cb.id; label.textContent=model;
    row.append(cb,label); host.appendChild(row);
  });
}
async function loadRoutes() {
  const result = await api('GET', '/admin/model-routes');
  if (adminReady(result)) pretty('#routeList', result.data.routes || []);
}
function renderManagementRows(target, rows, kind) {
  const host=$(target); host.replaceChildren();
  if (!rows.length) { host.textContent='沒有資料。'; return; }
  rows.forEach(rec => {
    const row=document.createElement('div'); row.className='model-item';
    const label=document.createElement('span'); label.style.flex='1';
    label.textContent = kind==='key'
      ? `${rec.label || '未命名'} · ${rec.key_id} · ${rec.allow_public?'對外':'對內'} · ${rec.enabled?'啟用':'停用'}`
      : `${rec.username} · ${rec.role} · ${rec.disabled?'停用':'啟用'} · ${rec.uid}`;
    row.appendChild(label);
    if (kind==='key' && rec.enabled && rec.key_id!=='admin') {
      const disable=document.createElement('button'); disable.textContent='停用';
      disable.onclick=async()=>{const r=await api('POST','/admin/keys',{action:'disable',key_id:rec.key_id}); if(adminReady(r))loadKeys();}; row.appendChild(disable);
      const del=document.createElement('button'); del.textContent='刪除';
      del.onclick=async()=>{if(!confirm(`刪除金鑰 ${rec.key_id}？`))return; const r=await api('POST','/admin/keys',{action:'delete',key_id:rec.key_id}); if(adminReady(r))loadKeys();}; row.appendChild(del);
    }
    if (kind==='account' && !rec.disabled) {
      const reset=document.createElement('button'); reset.textContent='重設密碼';
      reset.onclick=async()=>{const password=prompt(`輸入 ${rec.username} 的新密碼（至少 12 字元）`);if(password===null)return;const r=await api('POST','/admin/accounts',{action:'password',uid:rec.uid,password});if(adminReady(r))showMsg('密碼已更新',true);}; row.appendChild(reset);
      const disable=document.createElement('button'); disable.textContent='停用';
      disable.onclick=async()=>{const r=await api('POST','/admin/accounts',{action:'disable',uid:rec.uid}); if(adminReady(r))loadAccounts();}; row.appendChild(disable);
    }
    host.appendChild(row);
  });
}
async function loadKeys() { const r=await api('GET','/admin/keys'); if(adminReady(r))renderManagementRows('#keyList',r.data.keys||[],'key'); }
async function loadAccounts() { const r=await api('GET','/admin/accounts'); if(adminReady(r))renderManagementRows('#accountList',r.data.accounts||[],'account'); }
async function loadAccess() {
  const url=$('#clientApiUrl'), status=$('#clientApiStatus');
  if(!url||!status)return;
  if(!state.base){ACCESS_VIEW.apiBase='';ACCESS_VIEW.public=false;ACCESS_VIEW.publicBase='';ACCESS_VIEW.internalBase='';url.textContent='尚未設定';status.textContent='完成上游設定後即可檢查 API 位址。';return;}
  url.textContent='檢查中…';
  const r=await api('GET','/access');
  if(!r.ok){url.textContent='無法取得';status.textContent='位址檢查失敗（HTTP '+r.status+'）。';ACCESS_VIEW.apiBase='';ACCESS_VIEW.public=false;ACCESS_VIEW.publicBase='';ACCESS_VIEW.internalBase='';}
  else {
  const addresses=r.data.addresses||[];
  const pub=addresses.find(a=>a.kind==='public'&&a.reachable);
  const local=addresses.find(a=>a.kind==='loopback'&&a.reachable);
  const lan=addresses.find(a=>a.kind==='lan'&&a.reachable);
  ACCESS_VIEW.public=!!pub;
  ACCESS_VIEW.publicBase=pub?.base_url||'';
  ACCESS_VIEW.internalBase=(lan||local)?.base_url||'';
  ACCESS_VIEW.apiBase=ACCESS_VIEW.publicBase||ACCESS_VIEW.internalBase;
  url.textContent=ACCESS_VIEW.apiBase||'沒有可用位址';
  status.textContent=pub
    ? '公開隧道已驗證；其他網路與裝置可使用此網址，推論仍需客戶端金鑰。'
    : (r.data.public_configured || r.data.configured_public_base
      ? '公開網址目前無法連線；對內網址只能在本機或同一內網使用。'
      : (lan ? '目前提供本機／區網網址；其他網路無法連線。' : '目前只有本機網址；其他裝置無法連線。'));
  }
  const resultUrl=$('#outUrl'), hint=$('#outUrlHint'), pair=$('#btnCopyClientConfig');
  if(resultUrl&&state.configured){
    const scope=state.keyScope||'public';
    resultUrl.textContent=clientBaseForScope(scope)||'尚無可用的此範圍網址';
    hint.textContent=scope==='public'
      ? (ACCESS_VIEW.public?'對外金鑰；公開網址已驗證，可跨網路使用。':'此金鑰允許對外使用；公開網址尚未驗證。')
      : '對內金鑰；只允許本機或內網呼叫。';
    pair.disabled=!state.key||!clientBaseForScope(scope)||(scope==='public'&&!ACCESS_VIEW.public);
    pair.title=scope==='public'&&!ACCESS_VIEW.public?'尚無已驗證的公開網址':(!state.key?'目前沒有一次性顯示的客戶端金鑰':'');
  }
}
async function copyText(value, success) {
  try { await navigator.clipboard.writeText(value); showMsg(success,true); }
  catch { showMsg('無法存取剪貼簿，請選取網址或金鑰後複製。',false); }
}
function showCreatedKey(token, scope) {
  const host=$('#newKeyResult'); host.replaceChildren(); host.style.display='block';
  const title=document.createElement('strong'); title.textContent='客戶端金鑰已建立，只會顯示這一次';
  const value=document.createElement('code'); value.className='key-token'; value.textContent=token;
  const base=clientBaseForScope(scope);
  const external=scope==='public';
  const actions=document.createElement('div'); actions.className='action-row';
  const copyKey=document.createElement('button'); copyKey.textContent='複製金鑰';
  copyKey.onclick=()=>copyText(token,'已複製客戶端金鑰');
  const copyPair=document.createElement('button'); copyPair.className='primary'; copyPair.textContent=external?'複製公開網址 + 金鑰':'複製內網網址 + 金鑰';
  copyPair.disabled=!base||(external&&!ACCESS_VIEW.public);
  copyPair.title=external&&!ACCESS_VIEW.public?'請先啟動並驗證公開隧道':'';
  copyPair.onclick=()=>copyText(`Base URL: ${base}\nAPI Key: ${token}`,external?'已複製，可貼到其他網路的 API 客戶端':'已複製內網 API 設定');
  actions.append(copyKey,copyPair);
  const hint=document.createElement('small'); hint.className='field-hint';
  hint.textContent=external&&!ACCESS_VIEW.public
    ? '對外權限已設定，但公開網址尚未驗證；伺服器只保存金鑰雜湊，請立即複製金鑰。'
    : `範圍：${external?'對外':'本機／內網'}。伺服器只保存金鑰雜湊；請立即複製，離開頁面後無法再次查看。`;
  host.append(title,value,actions,hint);
}
const TRACE_VIEW = { client: '', conv: '', selected: '', days: 7 };
function traceItem(title, meta, selected, onClick) {
  const button=document.createElement('button'); button.type='button';
  button.className='trace-item'+(selected?' sel':''); button.onclick=onClick;
  const strong=document.createElement('strong'); strong.textContent=title || '(無標題)';
  const small=document.createElement('small'); small.textContent=meta || '';
  button.append(strong,small); return button;
}
function traceDays() { return Number($('#traceDays')?.value || TRACE_VIEW.days || 7); }
async function loadTraceClients() {
  TRACE_VIEW.days=traceDays();
  const r=await api('GET',`/admin/trace/clients?days=${TRACE_VIEW.days}`);
  if(!adminReady(r))return;
  const host=$('#traceClients'); host.replaceChildren();
  const rows=r.data.clients||[];
  if(!rows.length){host.textContent='最近沒有回合紀錄。';return;}
  rows.forEach(item=>host.appendChild(traceItem(item.client,
    `${item.turns} 回合 · ${item.convs.length} 段對話 · ${item.last_when}`,
    item.client===TRACE_VIEW.client,()=>selectTraceClient(item.client))));
  $('#traceScope').textContent=`最近 ${TRACE_VIEW.days} 天 · ${rows.length} 個客戶端`;
}
async function selectTraceClient(client) {
  TRACE_VIEW.client=client; TRACE_VIEW.conv=''; TRACE_VIEW.selected='';
  $('#traceDetail').textContent='選擇回合以查看完整內容。';
  await Promise.all([loadTraceClients(),loadTraceConversations(),loadTraceTurns()]);
}
async function loadTraceConversations() {
  if(!TRACE_VIEW.client){$('#traceConversations').textContent='選擇客戶端';return;}
  const r=await api('GET',`/admin/trace/conversations?days=${traceDays()}&client=${encodeURIComponent(TRACE_VIEW.client)}`);
  if(!adminReady(r))return;
  const host=$('#traceConversations'); host.replaceChildren();
  const rows=r.data.conversations||[];
  if(!rows.length){host.textContent='此客戶端沒有對話紀錄。';return;}
  rows.forEach(item=>host.appendChild(traceItem(item.first_user||item.conv,
    `${item.turns} 回合 · ${item.models.join(' / ')}${item.retries?` · 重試 ${item.retries}`:''} · ${item.last_when}`,
    item.conv===TRACE_VIEW.conv,()=>selectTraceConversation(item.conv))));
}
async function selectTraceConversation(conv) {
  TRACE_VIEW.conv=conv; TRACE_VIEW.selected='';
  $('#traceDetail').textContent='選擇回合以查看完整內容。';
  await Promise.all([loadTraceConversations(),loadTraceTurns()]);
}
async function loadTraceTurns() {
  const host=$('#traceTurns');
  const query=new URLSearchParams({days:String(traceDays()),limit:'60'});
  if(TRACE_VIEW.client)query.set('client',TRACE_VIEW.client);
  if(TRACE_VIEW.conv)query.set('conv',TRACE_VIEW.conv);
  const r=await api('GET','/admin/trace/turns?'+query.toString()); if(!adminReady(r))return;
  host.replaceChildren(); const rows=r.data.records||[];
  if(!rows.length){host.textContent='沒有符合的回合。';return;}
  rows.forEach(item=>{
    const retries=Math.max(0,(item.attempt_count||0)-1);
    const meta=`${item.model||'?'} · ${item.verdict||'—'} · ${item.final_chars||0} 字${retries?` · 重試 ${retries}`:''} · ${item.when||''}`;
    host.appendChild(traceItem(`${item.when||''}　${item.user||'(無使用者文字)'}`,
      meta,item.id===TRACE_VIEW.selected,()=>loadTraceTurn(item.id)));
  });
}
function addTracePre(host,label,value) {
  const heading=document.createElement('div'); heading.className='field-label'; heading.textContent=label;
  const pre=document.createElement('pre'); pre.textContent=value || '(無文字)'; host.append(heading,pre);
  return pre;
}
async function loadTraceTurn(id) {
  TRACE_VIEW.selected=id; loadTraceTurns();
  const r=await api('GET','/admin/trace/turns/'+encodeURIComponent(id)); if(!adminReady(r))return;
  const rec=r.data.record||{}, host=$('#traceDetail'); host.replaceChildren();
  const title=document.createElement('h3'); title.textContent=`${rec.when||''} · ${rec.model||'?'} · ${rec.verdict||'—'}`; host.appendChild(title);
  const info=document.createElement('p');
  info.className='hint';
  info.textContent=`客戶端 ${rec.client||'?'} · 對話 ${rec.conv||'?'} · ${rec.msgs||0} 則訊息 · ${rec.elapsed||0}s · ${rec.attempts?.length||0} 次上游呼叫`;
  host.appendChild(info);
  const usage=[];
  if(rec.prompt_tokens!=null)usage.push(`輸入 ${rec.prompt_tokens}${rec.cached_tokens?`（快取 ${rec.cached_tokens}）`:''}`);
  if(rec.completion_tokens!=null)usage.push(`輸出 ${rec.completion_tokens}`);
  if(rec.reasoning_tokens)usage.push(`思考 ${rec.reasoning_tokens}`);
  if(rec.tool_calls)usage.push(`工具 ${rec.tool_calls} 次${rec.tool_names?.length?'：'+rec.tool_names.join(', '):''}`);
  if(usage.length){const line=document.createElement('p');line.className='hint';line.textContent='用量：'+usage.join(' · ');host.appendChild(line);}
  addTracePre(host,'使用者',rec.user);
  if(rec.think)addTracePre(host,'模型思考',rec.think);
  const attempts=rec.attempts||[];
  if(attempts.length){
    const heading=document.createElement('div');heading.className='field-label';heading.textContent='每次嘗試';host.appendChild(heading);
    attempts.forEach(a=>{
      const detail=document.createElement('details');detail.className='trace-attempt';
      const summary=document.createElement('summary');
      summary.textContent=`#${a.n} ${a.kind||'?'} · ${a.chars||0} 字 · HTTP ${a.status||'—'} · +${a.at||0}s${a.note?' · '+a.note:''}`;
      const pre=document.createElement('pre');pre.textContent=a.text||'(無文字)';detail.append(summary,pre);host.appendChild(detail);
    });
  }
  const finalPre=addTracePre(host,'最終回覆',rec.final);
  if(rec.final){
    const actions=document.createElement('div');actions.className='action-row';
    const copy=document.createElement('button');copy.textContent='複製回覆';
    copy.onclick=()=>navigator.clipboard.writeText(rec.final).then(()=>showMsg('已複製回覆',true)).catch(()=>showMsg('複製失敗',false));
    const full=document.createElement('button');full.textContent='全螢幕閱讀';
    full.onclick=()=>finalPre.requestFullscreen?.();
    actions.append(copy,full);host.appendChild(actions);
  }
  if(rec.error){const err=document.createElement('p');err.className='hint';err.textContent='錯誤：'+rec.error;host.appendChild(err);}
  if(rec.request){
    const details=document.createElement('details');details.className='trace-attempt';
    const summary=document.createElement('summary');summary.textContent='完整請求（包含工具定義）';
    const pre=document.createElement('pre');pre.textContent=JSON.stringify(rec.request,null,2);details.append(summary,pre);host.appendChild(details);
  }
}
async function loadUsage() { const r=await api('GET',`/admin/usage?days=${encodeURIComponent($('#usageDays').value||7)}`); if(adminReady(r))pretty('#usageData',r.data); }
async function loadAudit() { const r=await api('GET',`/admin/audit?days=${encodeURIComponent($('#usageDays').value||7)}`); if(adminReady(r))pretty('#auditData',r.data); }
async function loadRequestLogs() { const r=await api('GET','/admin/request-logs?limit=200'); if(adminReady(r))pretty('#requestLogData',r.data.entries||[]); }

document.addEventListener('DOMContentLoaded', function () {
  $('#sourceSelect').onchange=()=>fillSource($('#sourceSelect').value);
  $('#btnSourceSave').onclick=async()=>{const r=await sourceAction('save'); if(r){$('#sourceSecret').value='';await loadSources();fillSource(r.source?.id||$('#sourceId').value.trim());}};
  $('#btnSourceRefresh').onclick=loadSources;
  $('#btnSourceDiscover').onclick=async()=>{const r=await sourceAction('discover');if(r)renderSourceModels(r.models);};
  $('#btnSourceTest').onclick=async()=>{const model=$('#sourceModelList input:checked')?.value||'';const r=await sourceAction('test',{model});if(r)pretty('#routeList',r);};
  $('#btnSourcePublish').onclick=async()=>{const models=$$('#sourceModelList input:checked').map(cb=>cb.value);const profile=$('#sourceProfile').value;const profiles={};models.forEach(m=>{if(profile)profiles[m]=profile;});const r=await api('POST','/admin/sources/publish',{source_id:selectedSource(),models,profiles});if(adminReady(r)){pretty('#routeList',r.data);await loadSources();}};
  $('#btnSourceDelete').onclick=async()=>{const id=selectedSource();if(!id||!confirm(`刪除來源 ${id} 及其發布路由？`))return;const r=await sourceAction('delete');if(r)loadSources();};
  $('#btnKeyCreate').onclick=async()=>{const r=await api('POST','/admin/keys',{action:'create',label:$('#newKeyLabel').value,scope:$('#newKeyScope').value});if(adminReady(r)){await loadAccess();showCreatedKey(r.data.key.token,r.data.key.scope);$('#newKeyLabel').value='';loadKeys();}};
  $('#btnKeysRefresh').onclick=loadKeys;
  $('#btnAccessReload').onclick=loadAccess;
  $('#btnCopyApiUrl').onclick=()=>ACCESS_VIEW.apiBase&&copyText(ACCESS_VIEW.apiBase,'已複製 API 位址');
  $('#btnAccountCreate').onclick=async()=>{const r=await api('POST','/admin/accounts',{action:'create',username:$('#newAccountName').value,password:$('#newAccountPassword').value,role:'owner'});if(adminReady(r)){showMsg('帳號已建立',true);$('#newAccountPassword').value='';loadAccounts();}};
  $('#btnAccountsRefresh').onclick=loadAccounts;
  $('#btnTraceRefresh').onclick=async()=>{await loadTraceClients();if(TRACE_VIEW.client){await loadTraceConversations();await loadTraceTurns();}};
  $('#traceDays').onchange=async()=>{TRACE_VIEW.days=traceDays();await loadTraceClients();if(TRACE_VIEW.client){await loadTraceConversations();await loadTraceTurns();}};
  $('#btnRequestLogs').onclick=loadRequestLogs;
  $('#btnUsage').onclick=loadUsage; $('#btnAudit').onclick=loadAudit;
  $('#btnReplay').onclick=async()=>{const r=await api('POST','/admin/replay',{conversation_id:$('#replayConversation').value,prompt:$('#replayPrompt').value||undefined});if(adminReady(r))pretty('#replayData',r.data);};
  $('#btnCompare').onclick=async()=>{const models=$('#compareModels').value.split(',').map(s=>s.trim()).filter(Boolean);const r=await api('POST','/admin/compare',{models,prompt:$('#comparePrompt').value});if(adminReady(r))pretty('#replayData',r.data);};
  $('#btnRetention').onclick=async()=>{const days=Number($('#retentionDays').value);if(!Number.isInteger(days)||days<1)return showMsg('請輸入大於 0 的天數',false);if(!confirm(`確定刪除 ${days} 天以前的記錄？此操作無法復原。`))return;const r=await api('POST','/admin/retention',{days,confirm:true});if(adminReady(r))showMsg('清理完成：'+JSON.stringify(r.data),true);};
  api('GET','/armor/registry').then(r=>{if(!r.ok)return;const select=$('#sourceProfile');select.replaceChildren();const fallback=document.createElement('option');fallback.value='';fallback.textContent='依模型指紋自動套用';select.appendChild(fallback);(r.data.families||r.data.profiles||[]).forEach(profile=>{const id=typeof profile==='string'?profile:profile.id;const option=document.createElement('option');option.value=id;option.textContent=typeof profile==='string'?profile:profile.label;select.appendChild(option);});});
});


/* ---- tuning ------------------------------------------------------------- */
const TUNING_GROUPS = {
  retry: '重試與評分',
  context: '上下文整理',
  memory: '摘要與狀態保留',
  capture: '記錄與存取',
  general: '其他',
};

async function loadTuning() {
  const { ok, data } = await api('GET', '/admin/tuning');
  if (!ok) { showMsg(t('unauthorized') + ': ' + (data.error && data.error.message || ''), false); return; }
  const host = $('#tuningFields');
  host.innerHTML = '';
  const grouped = new Map();
  (data.fields || []).forEach(field => {
    const key = field.group || 'general';
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(field);
  });
  for (const [group, fields] of grouped) {
    const card = document.createElement('section');
    card.className = 'tuning-group';
    const heading = document.createElement('h2');
    heading.className = 'card-heading';
    heading.textContent = TUNING_GROUPS[group] || TUNING_GROUPS.general;
    card.appendChild(heading);
    fields.forEach(field => {
      const row = document.createElement('div');
      row.className = field.type === 'bool' ? 'tuning-row tuning-toggle' : 'tuning-row';
      const label = document.createElement('label');
      const id = 'tune-' + field.name;
      label.htmlFor = id;
      label.textContent = field.label || field.name;
      const input = document.createElement('input');
      input.id = id;
      input.dataset.field = field.name;
      input.dataset.kind = field.type;
      if (field.type === 'bool') {
        input.type = 'checkbox';
        input.checked = Boolean(data.current[field.name]);
      } else {
        input.type = 'number';
        input.min = field.min;
        input.max = field.max;
        input.step = field.type === 'float' ? '0.1' : '1';
        input.value = data.current[field.name];
      }
      row.append(label, input);
      const bounds = field.type !== 'bool' && field.min !== undefined
        ? `範圍 ${field.min}–${field.max}` : '';
      if (field.note || bounds) {
        const help = document.createElement('small');
        help.className = 'field-hint';
        help.textContent = [field.note, bounds]
          .filter(Boolean).join(' · ');
        row.appendChild(help);
      }
      card.appendChild(row);
    });
    host.appendChild(card);
  }
  showMsg('已載入目前設定', true);
}

async function saveTuning() {
  const payload = {};
  $$('#tuningFields input').forEach(input => {
    payload[input.dataset.field] = input.dataset.kind === 'bool'
      ? input.checked : input.valueAsNumber;
  });
  const { ok, data } = await api('POST', '/admin/tuning', payload);
  showMsg(ok ? '設定已套用並保存' : (data.error && data.error.message || t('error')), ok);
  if (ok) await loadTuning();
}

document.addEventListener('DOMContentLoaded', function () {
  const load = $('#btnTuningLoad');
  const save = $('#btnTuningSave');
  if (load) load.onclick = loadTuning;
  if (save) save.onclick = saveTuning;
  // The armour picker is built from the server's registry, so the console never
  // hard-codes a list that drifts from blocks/registry.json.
  if (typeof loadArmorFamilies === 'function') loadArmorFamilies();
});
