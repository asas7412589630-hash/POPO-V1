# 帳戶制度 — 多帳戶、可見思考與重試的分享連結、金鑰不外流

> 這份文件說明 2026-11-06 加入的帳戶層：怎麼用、為什麼這樣設計、每個決定的
> 代價是什麼。程式在 `accounts.py` / `acct_http.py` / `console/account.js`。

---

## 這是什麼

三件原本沒有的東西：

| 功能 | 之前 | 現在 |
|---|---|---|
| **帳戶** | 一個共享 token，誰在用它不知道 | 多帳戶，各自持有金鑰、密碼、用量 |
| **看懂一輪對話** | 只有最終答案（其餘在 jsonl 裡翻） | 一個網址給別人看：提問、思考、被丟棄的失敗嘗試、重試次數 |
| **金鑰位置** | `proxy/keys.json` + 手貼鏡像 | 一樣在這台機器；帳戶只是加一層「這是誰的」 |

**沒有任何一個位元組離開這台裝置。** 密碼只存 PBKDF2 雜湊，金鑰只存 SHA-256，
回看頁由本機 trace 檔即時渲染，不經第三方服務、不呼叫外部 CDN。

---

## 上手（三步）

### 1. 建立第一個帳戶（只有一次）

```sh
cd /path/to/cinder-server      # 你解壓的目錄
./acct init
```

輸出範例：

```
owner 建立完成
  帳號: owner
  密碼: e-H4OKD-TLmACG9r
（此密碼只顯示這一次，登入後請改）
```

> 只能建立「第一個」。之後 `acct init` 會拒絕 —— 不然任何人跑到終端機就能
> 重設 owner。要加人走 `acct add`，或控制台 → 帳戶 → 建立帳戶。

也可以在控制台做：開 `/app/`，沒有帳戶時會直接出現「建立擁有者帳戶」的表單。

### 2. 建一把金鑰

控制台 → **帳戶** → 我的 API 金鑰 → 填標籤 → 建立金鑰。

**明文只顯示一次**（存的是雜湊，拿不回來）。建立後畫面上方會出現一張
高亮卡片，按「複製」即可。丟了就撤銷重建，不能找回。

### 3. 產生回看連結

控制台 → **帳戶** → 回看連結 → 選對話 → 選「對話 + 思考 + 重試」→ 產生。

或更快：**記錄** 頁每個對話列右邊有一個「分享」鈕，按下去直接複製連結。

網址長這樣：

```
https://<你的隧道>/i/v8GEa1xtJXMX3     含思考與重試
https://<你的隧道>/j/v8GEa1xtJXMX3     只有對話
```

對方不需要金鑰、不需要帳號，打開就看得到。**撤銷後立刻打不開**（不是靠到期）。

---

## 分享連結為什麼是「伺服器端記錄」而不是簽章 URL

簽章 URL（把內容或授權碼簽在網址裡）有三個不能接受的性質：

1. **撤銷不了**。簽出去就是簽出去了，只能等它過期。
2. **會外流**。網址會留在對方手機的瀏覽紀錄、截圖、聊天記錄裡。內容一旦
   編進 URL，就等於內容本身已經外流。
3. **看不到誰在看**。不知道被開了幾次、什麼時候開的。

所以這裡的做法是：網址裡只有一個 13 字元的隨機 `vid`（≈78 bits），內容留在
這台機器。`proxy/accounts.json` 裡一筆記錄就是一次授權，刪掉記錄 = 連結失效。
每一次開啟都會加一次 `hits`，你在控制台看得到「3 次點閱」。

### 兩種模式的差別

| | `/j/` 只有對話 | `/i/` 對話 + 思考 + 重試 |
|---|---|---|
| 你的提問 | ✅ | ✅ |
| 最終答案 | ✅ | ✅ |
| 模型的思考過程 | ❌ | ✅ |
| 被代理丟棄的失敗嘗試 | ❌ | ✅（原文，紅框） |
| 為什麼重試（判定理由） | ❌ | ✅ |
| 重試幾次、最後成不成功 | 只有次數 | ✅ |

「重試」那塊是這個功能的價值所在：代理會把拒答的第一次丟掉、換一種說法再問。
以前那個「被丟掉的拒答」只在 `runtime/trace/*.jsonl` 裡，得開終端機翻。
現在它就是回看頁上的一個紅框。

---

## 帳戶模型

```
使用者（users）
  ├─ id / name / role(owner|user) / enabled
  ├─ salt + PBKDF2-HMAC-SHA256(密碼, 90000 次)   ← 明文不存
  └─ 金鑰（tokens）
        ├─ id / label / prefix                    ← 明文不存，只存 keyring 的 SHA-256
        └─ enabled / last_used / hits             ← 用來回答「這把還在用嗎」

工作階段（sessions）
  └─ sha256(sid) → uid / 到期 / IP / UA          ← cookie 是 HttpOnly + SameSite=Lax

回看記錄（views）
  └─ vid → uid / kind(chat|think) / conv / 到期 / hits

用量（usage）
  └─ uid → 每日 {turns, tokens_in, tokens_out, chars_out}
```

三件刻意的事：

1. **金鑰的「真憑證」仍然由 `keyring.py` 管**。`accounts.py` 只記 `kid → uid`
   的歸屬。代理的驗證路徑（`_client_key_ok` / 鏡像檔 / 「停用記錄優先」）一個字
   都沒改 —— 認證這種地方，重寫就是製造「全部開放」或「全部鎖死」的 bug。

2. **`role=owner` 是唯一能管帳戶的角色**。它可以看所有人的金鑰與用量、建立/
   停用/刪除帳戶、改制度開關。`role=user` 只看得到自己。

3. **停用帳戶 = 撤銷它的一切**：所有 session、所有金鑰一起失效，回看連結也
   一起停。不需要一個一個收。

---

## 三個制度開關（owner 才能改）

控制台 → 帳戶 → 帳戶制度。

| 開關 | 預設 | 意思 |
|---|---|---|
| **金鑰必須屬於帳戶** | 關 | 開啟後，手貼進 `client_keys.txt` 的老金鑰會失效，只有從帳戶產生的能用。導入期建議先關，確認所有裝置都換過金鑰再開。 |
| **每個帳戶的金鑰上限** | 0（不限） | 超過就拒絕建立，錯誤訊息會說原因。 |
| 工作階段有效期 | 14 天 | 環境變數 `ARMOR_SESSION_TTL` |

`require_account` 之所以預設關閉：它是一個**會讓現有裝置突然不能用**的開關。
導入帳戶時最常見的失敗是「我剛剛建了金鑰，但舊手機連不上了」—— 因為舊手機
手上那把是手貼的、沒有主人。預設關閉讓兩件事可以分開做。

---

## 端點

### 帳戶 API（`/a/*`）— 需要 session cookie

登入後 cookie 是 `triad_sid`（HttpOnly + SameSite=Lax，HTTPS 時再加 Secure）。
**寫入另外要 `X-Armor-Admin: 1`**，這個標頭不在 CORS allow-list 裡，所以
跨來源頁面送不出來（送之前要 preflight，而 preflight 會失敗）。

| 方法 | 路徑 | 用途 |
|---|---|---|
| POST | `/a/login` | `{name, password}` → 設 cookie |
| POST | `/a/logout` | 撤銷這個 session |
| GET | `/a/me` | 我是誰、我的金鑰、我的連結、今日用量 |
| POST | `/a/password` | `{old, new}` → 換密碼（同時撤銷所有 session） |
| GET/POST | `/a/users` | 列 / 建 / 停用 / 啟用 / 重設 / 刪除（owner） |
| GET/POST/DELETE | `/a/keys` | 我的金鑰：列出 / 建立 / 撤銷 |
| GET/POST/DELETE | `/a/views` | 回看連結：列出 / 建立 / 撤銷 |
| GET | `/a/usage` | 每日用量（owner 可加 `?uid=`） |
| GET | `/a/conv` | 我的對話清單，或 `?conv=` 讀某一輪 |
| GET/DELETE | `/a/sessions` | 登入中的裝置 / 全部登出 |
| GET/POST | `/a/settings` | 制度開關（owner） |

### 公開頁（不需任何憑證）

| 路徑 | 內容 |
|---|---|
| `/j/<vid>` | 只有對話 |
| `/i/<vid>` | 對話 + 思考 + 被丟棄的重試 |

頁面本身不載入任何外部資源（CSP `default-src 'none'`，樣式 inline），所有文字
都經過 HTML 轉義 —— 對話內容裡的 `<script>` 只會顯示成文字。

---

## 終端機入口（`acct`）

網頁壞掉、忘記密碼、只想用 SSH 的時候用。

```sh
./acct init                          # 建立 owner（只一次）
./acct list                          # 所有帳戶
./acct add alice                     # 新帳戶，印出密碼
./acct pw alice                      # 重設密碼，印出新密碼
./acct enable|disable|del alice
./acct key alice --label phone       # 產生金鑰，印出明文（只此一次）
./acct keys alice                    # 只有 prefix
./acct link alice --conv ui-abc --kind think --ttl 7d
./acct views alice
./acct rm-view vXXXX
./acct usage alice --days 14
./acct stats
```

`acct link` 印出的網址會用 `ARMOR_PUBLIC_BASE` 或 `http://127.0.0.1:8412`。
要拿到隧道網址就在控制台產生（那裡會問執行中的代理）。

---

## 安全邊界（給未來的自己）

寫進來的東西會一直被讀，所以把「已經擋住什麼」寫清楚。

### 已擋

| 攻擊 | 為什麼擋住 |
|---|---|
| 跨來源讀 `/a/*` | CORS allow-list 只認同源 / `null` / 明確列出的來源。**不再反射 `Origin`**，也不再回 `Allow-Credentials`。 |
| 跨來源寫（CSRF） | 寫入要 `X-Armor-Admin: 1`，它不在 allow-headers → 一定 preflight → 一定失敗。 |
| XSS 偷 cookie | session cookie 是 HttpOnly，JS 讀不到。 |
| XSS 注入分享頁 | 所有插值都過 `html.escape`；CSP `default-src 'none'`。 |
| 暴力猜密碼 | 同 IP 8 次失敗鎖 10 分鐘；「帳號不存在」與「密碼錯」回同一個訊息、且都跑一次 PBKDF2（不然回應時間就是帳號列舉介面）。 |
| 離線爆破雜湊 | PBKDF2-HMAC-SHA256 90000 次（手機上 ~60ms/次）。 |
| 撿走手機上的 session | 換密碼 = 撤銷該帳戶所有 session；「登入中的裝置」頁看得到每一台。 |
| 猜分享連結 | vid 是 13 字元 `token_urlsafe(9)` ≈ 78 bits，且可以隨時撤銷。 |
| 分享頁洩漏別人的對話 | 連結帶 `uid`；記錄有明確 `uid` 且不同就不渲染。沒有 `uid` 的舊記錄只有 owner 能看到。 |
| 停用的帳戶繼續用 | 停用 → 撤銷所有 session + 金鑰 + 隱藏其回看連結。 |
| 金鑰明文外流 | 只存 SHA-256；`GET /keys`、`/a/me`、`/a/keys` 全部只回 `prefix`。 |

### 沒擋 / 要自己注意

1. **`/j/` `/i/` 是公開的。** 拿到網址的人就看得到內容，這是功能本身。連結會
   出現在對方手機的瀏覽紀錄、以及你貼出去的聊天室。**不要把回看連結貼進公開
   頻道** —— 它是「給特定的人看」，不是「發佈」。
2. **`require_account` 關著時，手貼的金鑰照樣能用。** 那是刻意的相容性，代價是
   這條路徑沒有「誰在用」的資訊。
3. **同一個 `127.0.0.1` 上的其他 app 可以連到代理。** 這在 `_authorized` 裡是
   既有設計（loopback 免金鑰），本層沒有改變它。要在意就開 `require_key`
   （預設已是開）並只開隧道出去。
4. **PBKDF2 是 CPU-bound。** 90000 次在低階機上可能 100ms+。要調用
   `ARMOR_PBKDF2_ITERS`，舊記錄保存自己的 iterations，改動不會讓人登不進去。
5. **`proxy/accounts.json` 是 0600。** 它含 salt 與雜湊 —— 不是明文，但仍然
   不該進備份雲端。

---

## 檔案位置

```
cinder_server/
  accounts.py         帳戶 / session / 金鑰歸屬 / 回看記錄 / 用量（21KB）
  acct_http.py        HTTP 介面 + 兩個公開頁的渲染（28KB）
  acct                終端機入口
  console/account.js  控制台「帳戶」分頁
  test_accounts.py    21 項：登入、鎖定、撤銷、歸屬、過期、壞檔
  test_view_page.py   30 項：兩種頁面的內容、撤銷、跨帳戶隔離、XSS
  test_account_e2e.py 端到端：假上游 → 真代理 → trace → 用量 → 回看頁

proxy/accounts.json   帳戶、session、金鑰歸屬、回看記錄、用量（0600）
proxy/keys.json       keyring 的金鑰記錄（SHA-256）
proxy/client_keys.txt keyring 寫的鏡像（enabled 的明文 token）
runtime/trace/*.jsonl 每一輪的完整記錄 —— 回看頁的資料來源
```

---

## 常見狀況

**「登入後每個按鈕都說請先登入」**
cookie 沒設起來。用 `http://127.0.0.1:PORT/app/`，不要用 `file://` 開
`index.html` —— `file://` 的 origin 是 `null`，cookie 存不進去。

**「建了金鑰但 client 說 401」**
控制台 → 帳戶 → 我的 API 金鑰，看那把是不是「啟用」。若是從舊的「金鑰」頁
建的、標籤寫「⚠ 沒有帳戶歸屬」，那它在 `require_account` 開啟後就會失效 ——
到「帳戶」頁重建一把。

**「分享連結打開是空白／沒有紀錄」**
回看頁只顯示**建立連結之後**寫入的回合？不是 —— 它讀 `runtime/trace/` 裡
該 `conv` 的所有回合（近 60 天）。空白通常是因為：這個 `conv` 的記錄屬於另一個
帳戶（隔離生效），或 `ARMOR_TRACE_DIR` 指到別的地方。

**「想整個重來」**
```sh
./triad-up --stop 2>/dev/null; rm proxy/accounts.json proxy/keys.json proxy/client_keys.txt
./triad-up && ./acct init
```
（金鑰會全滅，所有 client 要重貼。）

---

## 這次改動學到的三件事

1. **一次性明文不能被重繪蓋掉。** 第一版 `acctLoadAll()` 在貼上明文之後才跑，
   整個 `#acctBody` 重建 → 金鑰明文只存在 40 毫秒。使用者唯一的補救是再建一把。
   現在是「先重繪，再把明文插到最前面」，並用 `id` 去重（不然切分頁會長出兩張）。

2. **keep-alive 讓「這個 handler 算過了」變成錯的快取鍵。** `protocol_version`
   是 HTTP/1.1，同一個 Handler 實例服務多個請求。用 bool 記「uid 已解析」會讓
   第二個請求沿用第一個的 uid —— A 的對話記進 B 的帳。改成以**金鑰本身**為鍵。

3. **用量不能只在「下一次 bump 且過了節流窗口」才落盤。** 一輪對話結束就收
   app，那一輪的數字永遠不會出現。加了背景 flusher（5 秒）+ 關機強制 flush。
   計量表少算最後一筆，比計量表慢 5 秒糟得多。
