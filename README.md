**繁體中文** · [English](README.en.md)

<div align="center">

<img src="docs/banner.svg" alt="POPO — 給它一個上游 API，它還你一個自己的 API" width="100%">

<br>

**給它一個上游 API，它還你一個自己的 API。**

Python 標準庫 · OpenAI 相容 · Anthropic Messages 轉譯 · 多上游路由 · 可選提示積木 · 雙語控制台

<br>

![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-22c55e?style=flat-square)
![Version](https://img.shields.io/badge/version-2.7.19-22c55e?style=flat-square)
![License](https://img.shields.io/badge/license-CC%20BY--NC%204.0-c2410c?style=flat-square)

</div>

---

> ### ⚠️ 免責聲明（先讀）
>
> 本專案**僅供合法用途**：安全研究、滲透測試（需授權）、CTF、自家系統的技術問答、
> 個人學習與開發工作。
>
> **禁止**用於：入侵他人系統、竊取資料、製作或散播惡意程式、繞過他人服務的付費機制、
> 任何違反當地法律或服務條款的行為。
>
> 使用者對自己的所有操作負全責。作者不對任何誤用、資料損失、法律後果或第三方索賠負責。
> 專案按「現狀」提供，不附任何明示或暗示的保證。
>
> 詳細條款見 [LICENSE](LICENSE)（CC BY-NC 4.0）與 [SECURITY.md](SECURITY.md)。

---

## 這一版在做什麼

POPO 是一個**模型 API 閘道**。你給它一組上游憑證（位址 + 金鑰），它在本機起一個
**自己的 OpenAI 相容 API**，把上游的能力重新包一層：統一的模型名稱、可選的提示積木、
拒答自動重試、上下文整理、逐回合紀錄與調校開關。

```
你的客戶端  ──►  POPO（127.0.0.1:8412）  ──►  你的上游供應商
（任何 OpenAI      · 路由與模型別名              （OpenAI / OpenRouter /
  客戶端、SDK、     · 提示積木注入                 Groq / Ollama / 自架）
  curl、Claude Code）· 拒答重試與歷史整理
                    · 紀錄、稽核、回放
```

**換模型不用換客戶端。換客戶端不用重貼提示。換裝置只要接同一個網址。**

核心服務**只用 Python 標準庫**（`http.server` + `ssl` + `json`），沒有 `pip install`
這一步；控制台是純 HTML/JS，沒有建置流程、沒有外部 CDN。

---

## 快速開始

需求：**Python 3.10 以上**，核心服務不需要第三方套件。

```sh
python3 -m popo.server.app
```

```powershell
# Windows
py -3.10 -m popo.server.app
```

開啟 **http://127.0.0.1:8412/app/**

### 首次設定（四步）

| 步驟 | 做什麼 | 圖 |
|---|---|---|
| **1 · 上游 API** | 填 Base URL（結尾 `/v1`）與 API Key → 按「讀取模型清單」 | ↓ |
| **2 · 選模型與積木** | 勾要發布的模型，指定它吃哪一組提示積木 | ↓ |
| **3 · 對外或對內** | 只有自己用選「對內」（loopback 免金鑰） | ↓ |
| **4 · 套用** | 產出 **Base URL** 與一把 `triad-…` 用戶端金鑰 | ↓ |

![控制台 · 設定頁：填入上游位址與金鑰](docs/img/setup.png)

新產生的用戶端金鑰**只顯示一次**，請當場複製。之後任何客戶端：

```
Base URL : http://127.0.0.1:8412/v1
API Key  : triad-…
Model    : glm-5.3        （你勾選的其中一個）
```

---

## 它解決什麼

| 困擾 | 沒有它 | 有它 |
|---|---|---|
| 提示積木很長（3 萬多字） | 每個客戶端各貼一次，貼到爆 context | 代理按模型家族自動注入 |
| 模型拒答一次，整串對話就廢了 | 手動重開新對話 | 有界重試，每次換切入角度 |
| 客戶端被 CORS 擋 | 網頁版不能用 | 代理同源，全部可用 |
| 「它到底收到什麼？」 | 只能猜 | 診斷標頭 + 控制台預覽直接給出全文 |
| 上下文塞滿 | 手動刪訊息 | 壓縮／摺疊，兩種語意分開控制 |
| 多個客戶端各貼一次 | 維護地獄 | 全部接同一個位址 |
| 共用主機怕留下對話 | 到處都是 log | ephemeral 模式零落地 |

---

## 提示積木（可選）

`blocks/` 是純文字資料，`blocks/registry.json` 決定**哪個模型吃哪幾塊**。
程式裡沒有一行提示文字——你可以整批換成自己的稿，不需要改程式。

| 積木 | 內容 |
|---|---|
| `00_contract.txt` | 交付契約：短、結構化，任何模型都吃 |
| `10_cinder_nodebt.txt` | 人設（無思考預算條款，不強制署名） |
| `10_persona.txt` | 人設原始版（含思考預算條款） |
| `15_glm_53_delivery.txt` | GLM-5.3 輸出格式與工具參數校準 |
| `16_deepseek_v41_delivery.txt` | DeepSeek V4.1 Flash 結構化輸出校準 |
| `20_prefill.txt` | assistant 預填片段 |
| `30_gpt_guard.txt` | GPT 家族的 guard 條款 |

**明確選擇「不注入」（profile `off`）永遠可用**，通用稿 `native_contract` 是保底，
不是調校過的。改完積木不需要重啟打包，下一個請求就生效。

---

## 調校（進階設定）

控制台 **「調校」頁**，四組開關，每一欄都寫了它實際改什麼。因為 `compress`、
`fold` 這種名字，一旦誤開就會**無聲地改變回答**。

![控制台 · 調校頁：每一欄都有範圍與語意說明](docs/img/tuning.png)

### 重試與評分

| 設定 | 預設 | 說明 |
|---|---|---|
| `retry_on_refusal` | 開 | 看到拒答就換角度重問 |
| `retry_max` | 8 | 額外重試次數，不含第一次 → 最多 9 次上游呼叫 |
| `retry_budget_s` | 150 | 整輪重試的總時間上限 |
| `upstream_budget_s` | 90 | 單次上游呼叫的 timeout |

### 上下文整理

| 設定 | 預設 | 說明 |
|---|---|---|
| `compress` | 關 | **損失最小**：修剪工具輸出、保留 system 開頭與結尾，截去中段 |
| `fold` | 關 | **有損**：中段歷史換成摘要，原文只在紀錄中保留 |
| `compress_at_chars` / `compress_target_chars` | 55000 / 30000 | 何時開始壓、壓到多少 |
| `safe_trim` | 900 | 小於此長度的工具結果不修剪 |
| `fold_at_messages` / `fold_keep_turns` | 14 / 8 | 超過幾則開始摺疊、尾巴留幾輪原文 |
| `advertised_context` | 200000 | 回給 `/v1/models` 的 `context_window`；**不會限制上游** |
| `context_handoff_at` | 0（關） | 達門檻時請模型整理長任務交接文件 |
| `context_rotate_at` | 0（關） | 有交接摘要且達門檻時縮短歷史 |

> **壓縮與摺疊的差別**：壓縮是**修剪**（原文還在），摺疊是**摘要**（原文消失）。
> 要精確細節就只開壓縮。

### 摘要與狀態保留

`state_ledger`（開）跨對話記住已確認的標的、符號、位址與工具名稱，不重放舊提示全文；
`archive`（關）定期把對話快照寫到 `runtime/archive`；`artifact`（開）把回答中的程式碼
區塊存成檔案。

### 記錄與存取

`scrub_refusals`（開）把拒答回合從歷史刪掉；`scrub_prose`（關，預設關閉有實測理由：
保留敘述反而讓模型不用重新規劃）；`capture_responses` / `capture_full`（關）；
`require_client_key`（開，關掉等於開放代理）。

**超出範圍的值會被拒絕，不會自動夾住**——夾住會把打錯字藏起來。
需要重啟的欄位會明確拒絕並說明，整批驗證通過才套用。設定持久化到
`proxy/panel_settings.json`（0600），重啟不靜默回退。

---

## 紀錄與稽核

控制台 **「紀錄」頁**：載入對話、看逐回合快照、比對多模型、回放既有請求。

![控制台 · 紀錄頁：逐回合快照與回放比較](docs/img/records.png)

分層是 **客戶端 → 對話 → 回合**：展開可看到每一次嘗試（含被放棄的）與完整回覆。
記錄包含用量統計與管理稽核，可依保留期限清理（無法解析的資料不會被靜默刪除）。

---

## 主要功能

| 功能 | 說明 |
|---|---|
| **OpenAI Chat Completions 相容** | 標準 `/v1/chat/completions`，含真串流（TTFB 亞秒級） |
| **Anthropic Messages 轉譯** | `/v1/messages` 完整 SSE 生命週期：`message_start`、內容區塊起迄、文字／工具增量、`message_delta`、`message_stop`；`tool_use` / `tool_result` 正確往返，圖片 URL 與 base64 都會轉 |
| **直通端點** | `/v1/images/generations`、`/v1/embeddings`、`/v1/audio/transcriptions` 原樣轉發，不注入 |
| **多上游來源** | 不同模型接不同供應商，各自帶自己的金鑰；密鑰與路由分開儲存 |
| **模型別名與發布** | 只路由你勾選發布的模型；短別名（`glm`、`deepseek`）自動換成真 ID；**發布不刪裸別名** |
| **拒答處理** | 分類器 + 有界重試 + 品質評分；`truncated` 與 `refused` 分開（預算耗盡不重試、不假裝完整） |
| **上下文管理** | 壓縮／摺疊／長任務交接／摘要後輪替，四者可分別開關 |
| **身分與授權** | 用戶端金鑰（SHA-256 儲存，只顯示一次）、管理員帳戶（PBKDF2）、session 落盤、四層 gate、登入限流 |
| **安全邊界** | 本機同源管理需要**同時**通過 peer／Host／Origin／`Sec-Fetch-Site`／轉送標頭檢查；隧道來源不會取得免登入管理權；setup 探測有 egress 白名單（擋 loopback／private／metadata，DNS 解析後再檢查所有位址），且**授權在探測之前** |
| **併發上限** | 有界信號，等待有期限（超時 429 而不是無限排隊），slot 在 `finally` 釋放 |
| **診斷標頭** | `X-Armor-Request-Model` / `X-Armor-Upstream-Model` / `X-Armor-Policy-Len` / `X-Armor-Retries` / `X-Armor-Verdict` |
| **控制台** | 六頁（設定／來源／金鑰／紀錄／調校／說明），繁中／英文切換，PWA 可加到主畫面，零建置 |
| **ephemeral 模式** | `POPO_EPHEMERAL=1` 零落地；上游金鑰仍然**讀取**（它是輸入，不是紀錄） |

---

## 診斷：它到底做了什麼

每個回應都帶著：

```
X-Armor-Request-Model   glm
X-Armor-Upstream-Model  glm-5.3
X-Armor-Policy-Len      34059      ← 這次注入了多少字元（0 = 沒注入）
X-Armor-Retries         2          ← 燒了幾次重試
X-Armor-Verdict         retry_ok   ← 重試救回來了
```

想看模型真正會收到的文字（不發請求）：

```sh
curl "http://127.0.0.1:8412/armor?model=glm-5.3&level=native"
```

回的是**實際會送出的內容**，不是近似值。逐請求覆寫用 `X-Armor-Level`
（白名單外的值直接 400，不是靜默忽略）。

---

## 部署選項

| 佈局 | 對外 | 磁碟紀錄 | 適合 |
|---|---|---|---|
| 本機 | 只 loopback | 有 | 開發、自己用 |
| SSH 隧道 | 只 loopback | 有或無 | VPS，最安全 |
| **ephemeral** | 隧道或 LAN | **無** | 共用主機 |
| LAN | `0.0.0.0` | 有 | 家裡手機平板共用 |

主要環境變數（完整清單見 `popo/config.py` 的 `ENV_ALIASES`）：

```sh
POPO_BIND=127.0.0.1        # 監聽位址
POPO_PORT=8412             # 連接埠
POPO_REQUIRE_KEY=1         # 對外要求用戶端金鑰
POPO_EPHEMERAL=1           # 零落地
POPO_ARMOR=1               # 啟用提示積木
POPO_MAX_CONCURRENT=12     # 併發上限
POPO_ALLOW_PRIVATE_UPSTREAM=0   # 允許內網上游（預設擋）
```

`POPO_*` 與 1.x 的 `ARMOR_*` 名稱都認；環境變數優先於 `popo.json`。

完整說明：[docs/MANUAL.md](docs/MANUAL.md) · [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## 文件

| 文件 | 內容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 模組邊界、請求生命週期、控制台身分 |
| [FEATURE_MATRIX.md](FEATURE_MATRIX.md) | 功能狀態與**已知限制**（逐項附測試路徑） |
| [PARITY_REAL.md](PARITY_REAL.md) | 與 1.x 的行為差分（含刻意差異與理由） |
| [GOALS.md](GOALS.md) | 這一版的目標與非目標 |
| [REWRITE_PLAN.md](REWRITE_PLAN.md) | 重寫過程與取捨 |
| [OPEN_SOURCE_STATUS.md](OPEN_SOURCE_STATUS.md) | 授權狀態說明 |
| [docs/MANUAL.md](docs/MANUAL.md) | 完整使用說明書 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 部署佈局、systemd |
| [SECURITY.md](SECURITY.md) | 威脅模型（含「這個不防什麼」） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 貢獻規則 |
| [CHANGELOG.md](CHANGELOG.md) | 完整變更記錄 |

---

## 測試

```sh
./run-tests.sh          # 全部離線檢查
./run-tests.sh --online # 另外跑需要真上游的測試
```

每個階段都會讓整輪失敗——**靜默跳過的階段就等於不再保護任何東西**。

| 階段 | 內容 |
|---|---|
| `tools/architecture_check.py` | 架構紅線（300 行函式、700 行模組、無環、無 import-time env） |
| `tools/check_release_tree.py --allow-runtime` | 發行樹衛生（不得夾帶 `data/`、金鑰、日誌） |
| `compileall` | 位元組編譯 |
| `tests/unit` · `tests/integration` | 單元與 HTTP 整合 |
| `tests/parity/v1_v2_parity.py` | V2 環境契約 |
| `tests/parity/v1_diff.py` | 與 1.x 的**真**差分（`POPO_V1_DIR` 未設時明確回報 skipped，不是 passed） |

2.7.19 的驗證結果：**469 單元（5 skip）、54 整合（1 skip，無 ResourceWarning）、
22 parity、11/11 V2 環境檢查**；架構、發行樹、位元組編譯與 Console JavaScript 語法檢查通過。
**真實上游測試未隨本次發行執行。**

---

## 已知限制

- 提示積木只對受過驗證的家族調校過；其他模型走通用稿，**不保證效果**
- 稽核是 heuristic，不是正確性證明
- 無多租戶隔離、無負載測試（併發只到 12 路）
- 控制台雙語只涵蓋介面文字，積木內容本身是英文
- 上游若被 WAF 擋出網 IP，程式層救不了

逐項狀態見 [FEATURE_MATRIX.md](FEATURE_MATRIX.md)。

---

## 授權與版權

本專案採用 **[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/deed.zh-Hant)**：
**姓名標示 + 非商業性**。

### 版權所屬 (Copyright)

**Copyright © 2026 [asas7412589630-hash](https://github.com/asas7412589630-hash) & [Minglink](https://github.com/Minglink). All rights reserved.**

本專案由 [asas7412589630-hash](https://github.com/asas7412589630-hash) 與 [Minglink](https://github.com/Minglink) 共同創建與聯合維護。詳細作者與維護者名錄請參閱 [AUTHORS.md](AUTHORS.md)。

| 你可以 | 條件 |
|---|---|
| 自由使用、修改、分享 | **必須標明出處**（作者 + 原始連結） |
| 用在個人專案、研究、教學 | **不得用於商業目的** |
| 二次開發後再發布 | 需註明做了哪些變更，且維持同樣授權 |

**明確禁止商業使用。** 包括但不限於：拿去賣、包進付費產品、當成付費服務的後端、
放在有廣告的網站上營運。

### 商業授權

需要商業使用請來信洽談：**[asas7412589630@gmail.com](mailto:asas7412589630@gmail.com)**

### 檔案

| 檔案 | 內容 |
|---|---|
| [AUTHORS.md](AUTHORS.md) | 專案維護者與共創者名單 |
| [LICENSE](LICENSE) | CC BY-NC 4.0 英文法律原文（與 SPDX 官方逐位元組一致） |
| [LICENSE.zh-Hant.md](LICENSE.zh-Hant.md) | 繁體中文白話摘要（僅供理解，效力以英文為準） |
| [LICENSE.en.md](LICENSE.en.md) | 英文白話摘要 |

> **GitHub 側邊欄顯示「Other / NOASSERTION」是正常的。**
> GitHub 內建只認 13 種授權，**CC BY-NC 不在其中**，所以無法自動標示。
> 授權本身完全有效——以 `LICENSE` 檔的內容為準。

---

<div align="center">

**POPO** · 版權所有 © 2026 [asas7412589630-hash](https://github.com/asas7412589630-hash) & [Minglink](https://github.com/Minglink)

</div>
