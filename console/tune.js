/* Proxy tuning page.
 *
 * These are the switches the old /panel offered, moved to the console so the
 * operator never has to leave the app (and so the settings are reachable over
 * the tunnel, where /panel is now local-only). Two things matter here:
 *
 *   1. every control states what it does, because "scrub_prose" and "fold"
 *      decide whether a turn is retried or dropped from history — a silent
 *      toggle there changes answers invisibly;
 *   2. a failed write says so. The old panel flipped the button optimistically
 *      and left it flipped even when the POST failed.
 */

/* Shared state for this page. Declared here because every reference below
 * (`TUNE.cfg = c`) is a write to an undeclared global -> ReferenceError in
 * strict-ish scope, which killed the whole page: the switches and numbers never
 * rendered and the operator saw only a red "TUNE is not defined". The backend
 * was fine the whole time; only this declaration was missing. */
const TUNE = { cfg: null };

const SWITCH_GROUP = {
  retry_on_refusal: 'retry', retry_max: 'retry', retry_budget_s: 'retry',
  upstream_budget_s: 'retry',
  compress: 'context', fold: 'context',
  state_ledger: 'memory', archive: 'memory', artifact: 'capture',
  capture_responses: 'capture', capture_full: 'capture',
  scrub_refusals: 'capture', scrub_prose: 'capture', require_client_key: 'capture'
};
const GROUP_LABELS = { retry: '重試與評分', context: '上下文整理', memory: '摘要與狀態保留', capture: '記錄與存取' };

const SWITCH_LABEL = {
  compress: '上下文壓縮（長對話摘要回收）',
  fold: '滾動摺疊（舊回合併入摘要）',
  state_ledger: '狀態帳本（跨回合記住事實）',
  archive: '定期歸檔（寫入 runtime/state）',
  artifact: '成品落盤（程式碼區塊存檔）',
  capture_responses: '擷取回應全文（記錄頁可看全文）',
  capture_full: '擷取完整請求（含 tool 定義）',
  scrub_refusals: '清除拒絕歷史（不把拒答餵回模型）',
  scrub_prose: '清除敘述輪（只說要做、沒交付的回合）',
  retry_on_refusal: '拒絕自動重試',
  require_client_key: '對外要求金鑰',
};

const NUM_LABEL = {
  retry_max: '重試次數上限',
  retry_budget_s: '重試總預算（秒）',
  upstream_budget_s: '單次上游預算（秒）',
  context_handoff_at: '交接門檻（tokens，0=不限制）',
  context_rotate_at: '輪替門檻（tokens，0=不限制）',
  compress_at_chars: '壓縮觸發（字元，0=不限制）',
  compress_target_chars: '壓縮目標（字元）',
  safe_trim: '最小修剪（字元）',
  fold_at_messages: '摺疊訊息數',
  fold_keep_turns: '摺疊保留輪數',
  advertised_context: '宣告 context（tokens）',
};

async function tunePost(payload){
  const r = await adminFetch('/panel/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error('HTTP ' + r.status + (r.status === 401 ? '：金鑰無效' : ''));
  const d = await readJson(r);
  if (!d.ok) throw new Error(errText(d.error, 0));
  return d;
}

async function loadTune(){
  const swBox = $('#switchList');
  const numBox = $('#numberList');
  swBox.innerHTML = '<div style="color:var(--dim);font-size:12px">載入中…</div>';
  numBox.innerHTML = '';
  try {
    const r = await adminFetch('/panel/config');
    if (!r.ok) throw new Error('HTTP ' + r.status + (r.status === 401 ? '：需要金鑰' : ''));
    const c = await readJson(r);
    if (!c.ok) throw new Error(errText(c.error, 0));
    TUNE.cfg = c;

    const swEntries = Object.entries(c.switches || {});
    const grouped = {};
    for (const [k, v] of swEntries) (grouped[SWITCH_GROUP[k] || 'other'] ||= []).push([k, v]);
    swBox.innerHTML = Object.entries(grouped).map(([group, entries]) => `
      <section class="tune-group" aria-label="${esc(GROUP_LABELS[group] || '其他')}\">
        <h3 class="tune-group-title"><i class="dot"></i>${esc(GROUP_LABELS[group] || '其他')}</h3>
        ${entries.map(([k, v]) => `
          <div class="tune-row">
            <span>${esc(SWITCH_LABEL[k] || k)}</span>
            <button class="mini" data-sw="${esc(k)}" data-on="${v ? '1' : '0'}"
                    aria-pressed="${v ? 'true' : 'false'}"
                    style="${v ? 'color:var(--ok);border-color:#2a5a3d' : 'color:var(--dim)'}">
              ${v ? '啟用' : '停用'}</button>
          </div>`).join('')}
      </section>`).join('') || '<div style="color:var(--dim);font-size:12px">沒有可調開關</div>';

    const sw = c.switches || {}, nm = c.numbers || {};
    $('#tuneStatus').innerHTML = [
      `<span class="tune-chip ${sw.retry_on_refusal ? 'on' : ''}">自動重試 ${sw.retry_on_refusal ? '開' : '關'}</span>`,
      `<span class="tune-chip warn">最多重試 ${esc(nm.retry_max ?? '—')} 次</span>`,
      `<span class="tune-chip ${sw.compress ? 'on' : ''}">上下文壓縮 ${sw.compress ? '開' : '關'}</span>`,
      `<span class="tune-chip">觸發 ${esc(nm.compress_at_chars ?? '—')} 字</span>`
    ].join('');

    swBox.querySelectorAll('button[data-sw]').forEach(b => {
      b.onclick = async () => {
        const k = b.dataset.sw;
        const next = b.dataset.on !== '1';
        b.disabled = true;
        try {
          await tunePost({ kind: 'switch', key: k, value: next });
          toast((SWITCH_LABEL[k] || k) + ' → ' + (next ? '開' : '關'));
        } catch (e) {
          toast('設定失敗：' + (e.message || e));
        } finally {
          b.disabled = false;
          loadTune();      // re-read, so the button reflects what the server has
        }
      };
    });

    numBox.innerHTML = '<div class="tune-number-grid">' + Object.entries(c.numbers || {}).map(([k, v]) => `
      <div class="tune-num">
        <label for="num-${esc(k)}">${esc(NUM_LABEL[k] || k)}</label>
        <input type="number" id="num-${esc(k)}" data-num="${esc(k)}" value="${v ?? 0}">
      </div>`).join('') + '</div>' || '<div style="color:var(--dim);font-size:12px">沒有可調數值</div>';

    numBox.querySelectorAll('input[data-num]').forEach(inp => {
      inp.onchange = async () => {
        try {
          const d = await tunePost({ kind: 'number', key: inp.dataset.num,
                                     value: Number(inp.value) });
          inp.value = d.value;    // the server clamps to its own range
          toast((NUM_LABEL[inp.dataset.num] || inp.dataset.num) + ' = ' + d.value);
        } catch (e) {
          toast('設定失敗：' + (e.message || e));
        }
      };
    });

    $('#tuneUpstream').value = c.upstream_host || '';
    $('#tuneModelLock').value = c.model_lock || '';
    $('#tuneState').textContent = '· ' + (c.bind || '') + ' → ' + (c.upstream_host || '?');
  } catch (e) {
    swBox.innerHTML = '<div style="color:var(--bad);font-size:12px">' +
      esc(e.message || String(e)) + '</div>';
  }
}

$('#btnTuneSave').onclick = async () => {
  const out = $('#tuneOut');
  const up = $('#tuneUpstream').value.trim();
  const lock = $('#tuneModelLock').value.trim();
  out.textContent = '儲存中…';
  try {
    if (up) await tunePost({ kind: 'upstream_host', value: up });
    // Model lock: the endpoint rejects an empty value, and "clear the lock" is a
    // legitimate state, so an empty box means "leave as is" only when it already
    // was empty. Otherwise say plainly that clearing needs a restart.
    if (lock) {
      await tunePost({ kind: 'model_lock', value: lock });
      out.innerHTML = '✓ 已儲存。上游主機或模型鎖定變更要重新啟動代理才會生效。';
    } else {
      out.innerHTML = '✓ 已儲存上游主機。' +
        (TUNE.cfg && TUNE.cfg.model_lock
          ? '<br><span style="color:var(--warn)">模型鎖定仍為 ' +
            esc(TUNE.cfg.model_lock) + '，要解除需重啟並清空設定。</span>'
          : '');
    }
    loadTune();
  } catch (e) {
    out.innerHTML = '<span style="color:var(--bad)">失敗：' + esc(netErr(e)) + '</span>';
  }
};

/* Load on tab open, sharing the same hook pattern as the other pages. */
const _showTune = window.show;
window.show = function(v){
  _showTune(v);
  if (v === 'tune') loadTune();
};
