# POPO-V2 設立目標

> 以 POPO-V1 2.7.0 的**全部可交付功能**為行為基準，完成 POPO 2.x 全功能重寫。功能要完整移植，但只移植需求與可驗證行為，**不得複製 V1 的模組結構、超大型函式、全域耦合與技術債**。此目標未完成前，V2 不得宣稱與 V1 全功能等價。
>
> **總體目標：** 功能覆蓋 100%（每一項皆有契約與測試）、所有核心結構紅線為 0、發布物從乾淨解壓仍可重現測試。當「功能完整」與「結構乾淨」衝突時，拆分設計，不以略過功能或搬運舊程式碼解決。

---

## 一、為什麼要重建 V2

POPO-V1 已經驗證了核心想法：**在 API 層注入人設並自動重試拒答**，可以讓 GLM/DeepSeek/GPT 等模型持續交付技術內容。但 V1 同時累積了大量結構債：

| 問題 | 影響 |
|---|---|
| `armor_proxy.py` 9,469 行，`_forward_locked` 單函式 2,149 行 | 任何改動都牽一髮動全身，無法單元測試 |
| 功能零散分布在 `armor_*`/`subsystems`/`structural` 等模組之間 | 邊界不清，import 互相依賴 |
| 控制台 HTML 全擠在單一 `index.html` | UI 與 API 耦合，無法獨立演進 |
| 歷史包袱多（`cinder_server`、`armor_triad`、`kimi_relay`） | 難以確認哪個是最新可用版 |
| 大量測試依賴真上游 | 上游一 429 就全紅，開發者不信測試結果 |

**V2 的目標不是加功能，是把同樣的功能做得乾淨、可測、可移植。**

---

## 二、重建原則（不能妥協）

| # | 原則 | 檢驗標準 |
|---|---|---|
| P1 | **功能不變，結構重來** | V1 能交的東西 V2 也能交；但程式碼不是搬過來，是重新設計過 |
| P2 | **每個模組單一職責，有測試守護** | 核心模組都有獨立單元測試；測試失敗要能直接指出模組 |
| P3 | **純標準庫核心** | `popo/` 核心引擎必須只依賴 Python stdlib；console/包裝層才允許外部依賴 |
| P4 | **請求生命週期可追踪、可重放** | 從進來→注入→重試→送出→回應→清理，每步都要能被測試直接呼叫 |
| P5 | **Web / Desktop / APK 共用同一核心** | 網站與 APK 都是不同前端包同一個 `popo` 引擎；不為任何平台另開一套邏輯 |
| P6 | **零洩漏比功能多更重要** | `ephemeral` 模式下，唯一輸入是上游金鑰；所有紀錄都只有記憶體 |
| P7 | **有對照測試才算完成** | 每個功能都要有「V1 行為 vs V2 行為」對照測試，確保沒有漏掉 |
| P8 | **只移植功能契約，不複製 V1 結構** | 禁止引入 V1 的巨型代理檔/巨型函式、跨模組可變全域、import-time 設定快照；每個垂直功能有明確介面、單元與整合測試 |
| P9 | **發布物才是審計對象** | 只用白名單打包；最終 tarball 解壓後再掃描、編譯、測試；不得把運行設定、資料庫、日誌、憑證或本機狀態放進包 |
| P10 | **外部 I/O 可替換、可失敗** | upstream、clock、storage、transport 經明確依賴注入；timeout/cancel/error 都有契約，測試不依賴真上游 |
| P11 | **禁止靜默成功/靜默失敗** | 壞設定、缺能力、store 損壞及未支援端點必須有可辨識錯誤；不以空 dict、吞例外或假成功維持表面相容 |

---

## 三、功能基準（POPO-V1 必須全部保留）

### 3.1 代理核心

- [x] OpenAI `/v1/chat/completions` 相容代理
- [~] Anthropic `/v1/messages` ↔ OpenAI 雙向轉譯（基本轉換有；串流未轉譯）
- [ ] `/v1/models`、`/v1/images/generations`、`/v1/audio/transcriptions`、`/v1/embeddings` 原樣或相容轉發
- [ ] SSE 真串流：TTFB 亞秒級，不等到生成完（串流未做拒答重試 outer manager）
- [~] 上游連線池與 keep-alive 探測（簡易 pool，無健康檢查）
- [x] HTTP header 正規化（bot UA 改寫、`Accept-Language` 補齊）
- [ ] WAF/edge 封鎖偵測（`upstream.edge_blocked`）
- [~] 逾時與優雅斷線控管（逾時設定存在，優雅斷線未完整）

### 3.2 破甲系統

- [x] 人設區塊化注入：`blocks/*.txt` + `registry.json`
- [x] 模型家族自動配對：`glm`/`deepseek`/`gpt` 各走不同 profile
- [x] 系統提示插入點正確（OpenAI `messages[0]` / Anthropic `system`）
- [~] `X-Armor-Level: full/lean/off` 可覆蓋（API 未讀 header，僅 inject 支援）
- [ ] 區塊開頭哨兵（sentinels）剝除客戶端回傳的舊人設

### 3.3 拒答處理

- [~] 拒答分類：開頭拒絕、降級交付、空答案、技術內容補救（僅有 clean/refusal/capacity 三類）
- [x] 自動重試最多 8 次，每次換不同壓力角度
- [ ] 重試時輪替上游金鑰（多金鑰配額/帳號救生）
- [x] 重試結果透過 `X-Armor-Verdict` 標頭回報

### 3.4 歷史清理

- [x] 自動剝除對話歷史中的拒答回合
- [x] 保護最後一則 user 訊息不會被刪
- [x] 程式碼區塊視為有效內容，不會被誤判為拒答
- [x] 清理後若沒有 user 訊息，整體回滾

### 3.5 安全與帳戶

- [~] 用戶帳戶 + SHA-256 密碼雜湊 + session cookie（session 只記憶體，無 cookie）
- [x] 多把 triad- 用戶金鑰，可停用/撤銷/設標籤
- [~] 金鑰歸屬與用量分帳戶統計（寫入 log，無彙總 API）
- [ ] bootstrap setup token（首次安裝）
- [~] loopback / browser / operator / client-key 四層身分模型（僅 loopback + client-key）
- [~] admin 寫入動作需要真憑證，不是固定標頭（admin key 機制有，但無 multifactor）
- [ ] CORS 以伺服器端事實為準，不依賴 caller-controlled headers

### 3.6 控制台

- [x] 本地網頁控制台：設定、金鑰、對話、記錄、帳戶
- [ ] PWA 支援（manifest + icon + standalone）
- [ ] 多語言：`zh-Hant`、`zh-Hans`、`en`
- [x] 透過 `/armor?model=...` 直接看人設全文

### 3.7 執行模式

- [x] 本地模式（loopback）
- [x] LAN 模式（`0.0.0.0`）
- [~] ephemeral 零落地模式（record 已擋，admin_key 寫入已修）
- [~] SSH / cloudflare / srv.us 隧道腳本（腳本存在，未完整 E2E）
- [ ] systemd unit 產生

### 3.8 測試與部署

- [~] 離線測試矩陣（不依賴真上游）（基本 proxy + retry 有，覆蓋不完整）
- [x] real-upstream 可選測試開關
- [ ] CI workflow（GitHub Actions）
- [~] 一鍵啟動腳本 `triad-up`

---

### 3.9 V1 其他可交付功能（不可漏列）

此節補足功能基準中的周邊功能；每項移植前先確認 V1 實際行為，再決定 V2 契約與安全預設：

- [ ] `/models` 目錄/別名/發布清單/來源路由，禁止未發布模型誤暴露
- [ ] 多 upstream source、每來源密鑰、輪替與故障分類；不把憑證回顯給管理 API
- [ ] 記錄與 trace 查詢、清理/保留期限、回應/請求擷取開關及敏感欄位遮罩
- [ ] 對話記錄、回放、模型比較與成本/次數上限
- [ ] 配額/速率限制/並行上限/記憶體清理；取消客戶端請求時停止不必要的上游工作
- [ ] 控制台設定、模型/來源、金鑰、帳戶、對話、記錄、調校、診斷、Help/About 頁面的 V1 使用者工作流
- [ ] 導入/升級/備份/損壞狀態隔離與 schema migration，不得損壞時靜默清空
- [ ] 一鍵啟動/停止/重啟/狀態、部署檢查、隧道 URL 發現/失效清除與 systemd 產生
- [ ] V1 環境變數別名相容層；遇到舊項目的移除/更名都要明確記錄 migration
- [ ] V1 支援的 OpenAI/Anthropic request/response 形狀、工具呼叫、多模態欄位、SSE 終止、錯誤封裝與診斷 header
- [ ] 研究/外掛功能（如 adult/nude/media/model-map）逐項盤點：可交付的功能須移植；若安全或授權原因不移植，要有明確決策和替代方案，不能默默消失

## 四、V2 架構願景

```
POPO-V2/
├── popo/                     # 純 stdlib 核心引擎
│   ├── __init__.py
│   ├── config.py            # 環境變數 / 設定，單一真相來源
│   ├── errors.py            # 完整錯誤分類
│   ├── armor/               # 人設注入與拒答處理
│   │   ├── blocks.py        # 區塊載入、profile 解析
│   │   ├── inject.py        # 注入到 messages/system
│   │   ├── classify.py      # 拒答/降級/空答案分類
│   │   └── pressure.py      # 8 種重試壓力角度
│   ├── auth/                # 身分與金鑰
│   │   ├── accounts.py      # 帳戶與 session
│   │   ├── keys.py          # 用戶金鑰
│   │   └── gate.py          # loopback/browser/operator/client 判別
│   ├── core/                # 請求生命週期
│   │   ├── request.py       # 請求解析與正規化
│   │   ├── upstream.py      # 連線池、送出、串流讀取
│   │   ├── retry.py         # 重試控制器
│   │   ├── scrub.py         # 歷史清理
│   │   └── pipeline.py      # 組裝：注入→清理→送出→重試→回傳
│   ├── server/              # HTTP 服務層
│   │   ├── app.py           # BaseHTTPRequestHandler / 路由
│   │   ├── openai.py        # OpenAI 端點
│   │   └── anthropic.py     # Anthropic 轉譯
│   └── storage/             # 檔案與記憶體狀態
│       ├── ephemeral.py     # 全域 ephemeral 閘門
│       └── records.py       # log / trace / usage
├── console/                  # 前端控制台（標準 HTML/JS/CSS）
│   ├── index.html
│   ├── app.js
│   ├── i18n.js
│   └── style.css
├── tests/                    # 測試
│   ├── unit/                 # 各模組單元測試
│   ├── integration/          # 端到端 HTTP 測試
│   └── parity/               # V1 ↔ V2 行為對照
├── scripts/                  # 啟動與部署
│   ├── triad-up
│   ├── triad-deploy
│   └── triad-tunnel
├── README.md
└── CHANGELOG.md
```

---

## 五、完成定義

V2 只有在滿足以下條件時才算「乾淨重建完成」：

1. **核心模組獨立測試全綠**：`popo/` 下每個子模組都有 `tests/unit/test_*.py`，執行 `python -m unittest discover tests/unit` 全綠。
2. **整合測試全綠**：啟動本機代理後，對 `/v1/chat/completions` 等端點的請求與回應符合 OpenAI 形狀。
3. **功能對照測試全綠**：`tests/parity/v1_vs_v2.py` 針對 3.1–3.8 的功能清單逐項比對 V1 與 V2 輸出。
4. **全功能完成**：3.1–3.9 的每一項都須標示完成/不適用及理由，完成項有可執行測試；不得以功能相似、文件宣稱或測試數量推定移植完成。
5. **無結構債紅燈**：沒有任何單一函式超過 300 行；沒有循環 import；沒有 dead code；HTTP handler 不承載領域規則；無跨請求可變全域狀態；無 import-time env 讀取。
6. **可部署**：`./triad-up` 能在 Alpine/PRoot 上跑起來，且 console 可正常載入。
7. **ephemeral 驗證**：發送帶唯一標記的請求後，`grep -r` 整個工作目錄無命中。
   > 目前 `POPO_EPHEMERAL=1` 時 record 寫入已被擋住，且 `admin_key.txt` 不再寫出，
   > 但完整驗證尚未跑過（見 `PARITY_REAL.md` #9）。
8. **完成定義補充**：每個 `[x]` 必須有單元測試或整合測試可直接複驗，否則標為 `[~]` 或 `[ ]`。

---

## 六、不做的事（避免無限膨脹）

- 這一輪**不加新模型家族**，只保留 V1 已支援的 glm/deepseek/gpt。
- 這一輪**不做網站後端多租戶**，只保留 V1 的單機帳戶/金鑰模式。
- 這一輪**不做 APK**，但架構必須讓 APK 階段只替換 `console/` 與 `scripts/`，不動 `popo/`。

---

## 七、第一個可驗證里程碑（MVP-1，歷史階段）

> 此為早期骨架階段紀錄，不是目前完成標準；`app.py` 已超過 300 行紅線，需先拆分路由/handler 與應用服務。

- HTTP server adapter 啟動代理（handler 僅做 HTTP 解析/回應）
- 支援不帶 armor 的 `/v1/chat/completions` 轉發
- 有一個假的 upstream server
- 測試：對照 V1 與 V2 對同一個 upstream 的回應 JSON

從这里開始，逐步把 3.1–3.8 的功能一層層加回去，每層都有對照測試。

---

**下一步：建立骨架 + 最小可跑代理。**
