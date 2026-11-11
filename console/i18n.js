'use strict';

/**
 * Translations.
 *
 * Both dictionaries must have exactly the same keys — a missing key falls back
 * to the key name, which shows up as raw English in a Chinese UI and is easy to
 * miss. `tests/unit/test_console_i18n.py` runs this file and compares the key
 * sets, so the failure is caught in CI rather than by a user.
 */
const I18N = {
  'zh-Hant': {
    appTitle: 'POPO 控制台',
    setup: '設定',
    sources: '來源',
    manage: '管理',
    records: '紀錄',
    about: '說明',
    chat: '對話',
    keys: '金鑰',
    history: '紀錄',
    armor: '破甲',
    baseUrl: 'Base URL',
    apiKey: 'API Key',
    adminKey: 'Admin Key',
    username: '帳號',
    password: '密碼',
    login: '登入',
    logout: '登出',
    save: '保存',
    model: '模型',
    armorLevel: '破甲等級',
    send: '送出',
    ready: '就緒',
    sending: '傳送中…',
    error: '錯誤',
    needKey: '需要 API Key',
    unauthorized: '未授權',
    noKeys: '尚無金鑰',
    noConversations: '尚無對話紀錄',
    label: '標籤',
    enabled: '啟用',
    disabled: '禁用',
    deleteBtn: '刪除',
    createKey: '建立金鑰',
    identity: '身分',
    upstreamHost: '上游位址',
    upstreamKey: '上游金鑰',
    setupToken: '首次設定碼',
    readModels: '讀取模型清單',
    testConnection: '測試連線',
    apply: '套用',
    publish: '發布模型',
    preview: '預覽',
    copy: '複製',
    copied: '已複製',
    conversation: '對話',
    requestCount: '請求數',
    tokensIn: '輸入 tokens',
    tokensOut: '輸出 tokens',
    loading: '載入中…',
    none: '無',
    tuning: '調教',
    tuningHint: '分組調整重試、上下文壓縮和存取規則；變更即時生效並保存。',
    loadTuning: '讀取',
  },
  en: {
    appTitle: 'POPO Console',
    setup: 'Setup',
    sources: 'Sources',
    manage: 'Manage',
    records: 'Records',
    about: 'About',
    chat: 'Chat',
    keys: 'Keys',
    history: 'History',
    armor: 'Armor',
    baseUrl: 'Base URL',
    apiKey: 'API Key',
    adminKey: 'Admin Key',
    username: 'Username',
    password: 'Password',
    login: 'Login',
    logout: 'Logout',
    save: 'Save',
    model: 'Model',
    armorLevel: 'Armor level',
    send: 'Send',
    ready: 'Ready',
    sending: 'Sending…',
    error: 'Error',
    needKey: 'API key required',
    unauthorized: 'Unauthorized',
    noKeys: 'No keys yet',
    noConversations: 'No conversations yet',
    label: 'Label',
    enabled: 'Enabled',
    disabled: 'Disabled',
    deleteBtn: 'Delete',
    createKey: 'Create key',
    identity: 'Identity',
    upstreamHost: 'Upstream host',
    upstreamKey: 'Upstream key',
    setupToken: 'Setup token',
    readModels: 'Load model list',
    testConnection: 'Test connection',
    apply: 'Apply',
    publish: 'Publish models',
    preview: 'Preview',
    copy: 'Copy',
    copied: 'Copied',
    conversation: 'Conversation',
    requestCount: 'Requests',
    tokensIn: 'Input tokens',
    tokensOut: 'Output tokens',
    loading: 'Loading…',
    none: 'None',
    tuning: 'Tuning',
    tuningHint: 'Adjust retry, context compression and access in groups. Changes take effect immediately and are saved.',
    loadTuning: 'Load',
  },
};

const LANG_STORAGE = 'popo_lang';
const SUPPORTED = Object.keys(I18N);

let currentLang = 'zh-Hant';

function detectLang() {
  try {
    const saved = localStorage.getItem(LANG_STORAGE);
    if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;
  } catch (e) { /* storage may be blocked */ }
  const nav = (navigator.language || '').toLowerCase();
  if (nav.indexOf('zh') === 0) return nav.indexOf('cn') !== -1 || nav.indexOf('hans') !== -1
    ? 'zh-Hant' : 'zh-Hant';
  return 'en';
}

function t(key) {
  const set = I18N[currentLang] || I18N['zh-Hant'];
  if (Object.prototype.hasOwnProperty.call(set, key)) return set[key];
  const fallback = I18N['zh-Hant'];
  return Object.prototype.hasOwnProperty.call(fallback, key) ? fallback[key] : key;
}

function applyTranslations(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-i18n]').forEach(function (node) {
    node.textContent = t(node.getAttribute('data-i18n'));
  });
  scope.querySelectorAll('[data-i18n-placeholder]').forEach(function (node) {
    node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder')));
  });
  document.documentElement.lang = currentLang;
  document.title = t('appTitle');
}

function setLang(lang) {
  currentLang = SUPPORTED.indexOf(lang) !== -1 ? lang : 'zh-Hant';
  try { localStorage.setItem(LANG_STORAGE, currentLang); } catch (e) { /* ignore */ }
  applyTranslations();
  document.dispatchEvent(new CustomEvent('popo:langchange', { detail: currentLang }));
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { I18N, t, setLang, applyTranslations, detectLang, SUPPORTED };
}
