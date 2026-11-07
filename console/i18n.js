/* Forge Client — i18n Internationalization Engine
 * Supports Traditional Chinese (zh-Hant), Simplified Chinese (zh-Hans), and English (en).
 */
(function() {
  const STORAGE_KEY = 'forge_lang_v1';
  const LANGS = ['zh-Hant', 'zh-Hans', 'en'];

  const DICT = {
    'zh-Hant': {
      'tab.chat': '對話',
      'tab.log': '記錄',
      'tab.keys': '金鑰',
      'tab.setup': '設定',
      'tab.tune': '調校',
      'tab.account': '帳戶',
      'tab.about': '說明',
      'status.unconfigured': '未設定',
      'status.connecting': '連線中…',
      'status.ok': '正常',
      'status.error': '錯誤',
      'armor.label': '破甲',
      'armor.loading': '載入中…',
      'chat.empty': '尚未開始對話。在下方輸入訊息，或到「設定」連接你的模型代理。',
      'chat.placeholder': '輸入訊息… (Ctrl+Enter 送出)',
      'btn.send': '送出',
      'btn.stop': '中止',
      'btn.new': '新對話',
      'btn.save': '儲存',
      'btn.reset': '重設',
      'keys.mode_title': '連線模式',
      'keys.mode_current': '目前模式',
      'keys.mode_local': '對內（免金鑰）',
      'keys.mode_remote': '對外（需金鑰）',
      'keys.hint_local': '任何能連到這個 port 的人都能直接使用，不需要金鑰。只適合純本機。',
      'keys.hint_remote': '任何連到這個網址的請求都必須帶金鑰。開過隧道就該用這個。',
      'keys.go_wizard': '到「設定」改模式',
      'keys.info_title': '連線資訊',
      'keys.btn_create': '建立新金鑰',
      'keys.label_ph': '金鑰標籤 (例如：手機、工作機)',
      'account.login_title': '帳戶登入',
      'account.username': '使用者名稱',
      'account.password': '密碼',
      'account.btn_login': '登入',
      'account.btn_logout': '登出',
      'account.modal_title_bootstrap': '建立擁有者帳戶',
      'account.modal_title_login': '登入',
      'account.hint_bootstrap': '這個代理還沒有任何帳戶。建立第一個 —— 它就是擁有者，可以再開其他帳戶。',
      'account.hint_login': '帳戶只存在你自己的代理裡，密碼不會上傳到任何地方。',
      'account.btn_bootstrap': '建立並登入',
      'account.btn_skip': '稍後設定，直接使用控制台',
      'account.btn_close': '關閉',
      'account.err_missing_name': '請填帳號',
      'account.err_short_pw': '密碼至少 8 個字元',
      'setup.title': '上游供應商與模型設定',
      'setup.base_url': '上游 API 位址 (Base URL)',
      'setup.api_key': '上游 API 金鑰',
      'setup.load_models': '讀取模型清單',
      'setup.save_apply': '儲存並套用'
    },
    'zh-Hans': {
      'tab.chat': '对话',
      'tab.log': '记录',
      'tab.keys': '密钥',
      'tab.setup': '设置',
      'tab.tune': '调优',
      'tab.account': '账户',
      'tab.about': '说明',
      'status.unconfigured': '未设置',
      'status.connecting': '连接中…',
      'status.ok': '正常',
      'status.error': '错误',
      'armor.label': '破甲',
      'armor.loading': '加载中…',
      'chat.empty': '尚未开始对话。在下方输入消息，或前往“设置”连接你的模型代理。',
      'chat.placeholder': '输入消息… (Ctrl+Enter 发送)',
      'btn.send': '发送',
      'btn.stop': '中止',
      'btn.new': '新对话',
      'btn.save': '保存',
      'btn.reset': '重置',
      'keys.mode_title': '连接模式',
      'keys.mode_current': '当前模式',
      'keys.mode_local': '对内（免密钥）',
      'keys.mode_remote': '对外（需密钥）',
      'keys.hint_local': '任何能连接到该端口的访问均可直接使用，无需密钥。仅适合纯本地环境。',
      'keys.hint_remote': '所有发送至该地址的请求必须携带密钥。启用公开隧道时请务必使用此模式。',
      'keys.go_wizard': '前往“设置”修改模式',
      'keys.info_title': '连接信息',
      'keys.btn_create': '创建新密钥',
      'keys.label_ph': '密钥标签 (例如：手机、工作机)',
      'account.login_title': '账户登录',
      'account.username': '用户名',
      'account.password': '密码',
      'account.btn_login': '登录',
      'account.btn_logout': '登出',
      'account.modal_title_bootstrap': '创建所有者账户',
      'account.modal_title_login': '登录',
      'account.hint_bootstrap': '该代理尚未创建任何账户。创建第一个账户 —— 它将作为所有者，后续可创建其他账户。',
      'account.hint_login': '账户仅保存在本地代理中，密码绝不会上传至任何外部服务。',
      'account.btn_bootstrap': '创建并登录',
      'account.btn_skip': '稍后设置，直接使用控制台',
      'account.btn_close': '关闭',
      'account.err_missing_name': '请填写账号',
      'account.err_short_pw': '密码至少 8 个字符',
      'setup.title': '上游供应商与模型设置',
      'setup.base_url': '上游 API 地址 (Base URL)',
      'setup.api_key': '上游 API 密钥',
      'setup.load_models': '读取模型列表',
      'setup.save_apply': '保存并应用'
    },
    'en': {
      'tab.chat': 'Chat',
      'tab.log': 'Logs',
      'tab.keys': 'Keys',
      'tab.setup': 'Setup',
      'tab.tune': 'Tune',
      'tab.account': 'Account',
      'tab.about': 'Help',
      'status.unconfigured': 'Not configured',
      'status.connecting': 'Connecting…',
      'status.ok': 'Connected',
      'status.error': 'Error',
      'armor.label': 'Armor',
      'armor.loading': 'Loading…',
      'chat.empty': 'No conversation yet. Type a message below, or go to Setup to connect your upstream model.',
      'chat.placeholder': 'Type a message… (Ctrl+Enter to send)',
      'btn.send': 'Send',
      'btn.stop': 'Stop',
      'btn.new': 'New Chat',
      'btn.save': 'Save',
      'btn.reset': 'Reset',
      'keys.mode_title': 'Connection Mode',
      'keys.mode_current': 'Current Mode',
      'keys.mode_local': 'Local (No Key Required)',
      'keys.mode_remote': 'External (Key Required)',
      'keys.hint_local': 'Anyone who can connect to this port can use it directly without a key. Suitable for localhost only.',
      'keys.hint_remote': 'All requests to this URL must carry a key. Recommended whenever exposing via tunnel or public IP.',
      'keys.go_wizard': 'Go to Setup to change mode',
      'keys.info_title': 'Connection Details',
      'keys.btn_create': 'Create New Key',
      'keys.label_ph': 'Key label (e.g. Phone, Laptop)',
      'account.login_title': 'Account Login',
      'account.username': 'Username',
      'account.password': 'Password',
      'account.btn_login': 'Login',
      'account.btn_logout': 'Logout',
      'account.modal_title_bootstrap': 'Create Owner Account',
      'account.modal_title_login': 'Login',
      'account.hint_bootstrap': 'No accounts exist yet on this proxy. Create the first one — it will be the owner and can manage others.',
      'account.hint_login': 'Accounts exist only inside your local proxy. Passwords are never sent externally.',
      'account.btn_bootstrap': 'Create & Login',
      'account.btn_skip': 'Setup later, open console',
      'account.btn_close': 'Close',
      'account.err_missing_name': 'Username is required',
      'account.err_short_pw': 'Password must be at least 8 characters',
      'setup.title': 'Upstream Provider & Model Setup',
      'setup.base_url': 'Upstream Base URL',
      'setup.api_key': 'Upstream API Key',
      'setup.load_models': 'Load Model List',
      'setup.save_apply': 'Save & Apply'
    }
  };

  function detectLanguage() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved && LANGS.includes(saved)) return saved;
    } catch (_) {}
    const nav = (navigator.language || '').toLowerCase();
    if (nav.startsWith('zh-cn') || nav.startsWith('zh-sg') || nav.startsWith('zh-hans')) return 'zh-Hans';
    if (nav.startsWith('zh')) return 'zh-Hant';
    if (nav.startsWith('en')) return 'en';
    return 'zh-Hant';
  }

  let currentLang = detectLanguage();

  function t(key, fallback) {
    const langDict = DICT[currentLang] || DICT['zh-Hant'];
    return langDict[key] || fallback || key;
  }

  function applyDOM() {
    document.documentElement.lang = currentLang;

    document.querySelectorAll('[data-i18n]').forEach(el => {
      const k = el.dataset.i18n;
      const text = t(k);
      if (text) el.textContent = text;
    });

    document.querySelectorAll('[data-i18n-ph]').forEach(el => {
      const k = el.dataset.i18nPh;
      const text = t(k);
      if (text) el.setAttribute('placeholder', text);
    });

    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      const k = el.dataset.i18nTitle;
      const text = t(k);
      if (text) el.setAttribute('title', text);
    });

    const sel = document.getElementById('langSelect');
    if (sel && sel.value !== currentLang) {
      sel.value = currentLang;
    }
  }

  function setLang(lang) {
    if (!LANGS.includes(lang)) return;
    currentLang = lang;
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch (_) {}
    applyDOM();
    window.dispatchEvent(new CustomEvent('forge:langchange', { detail: { lang } }));
  }

  window.I18N = {
    t,
    setLang,
    getLang: () => currentLang,
    applyDOM,
    LANGS
  };

  document.addEventListener('DOMContentLoaded', () => {
    applyDOM();
    const sel = document.getElementById('langSelect');
    if (sel) {
      sel.value = currentLang;
      sel.addEventListener('change', () => setLang(sel.value));
    }
  });
})();
