# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

Two lineages live in this file:

1. **POPO 2.x** (below) — the modular rewrite that is the current code base.
   Entries describe the tree you are reading.
2. **Prism / Triad 1.x** ([archived at the end](#archive--prism--triad-1x)) —
   the previous single-file proxy. Kept verbatim for history; its files are no
   longer in this repository.

---

## Repository release — 2026-11-11 (tree = 2.7.19)

This is the first release published from this repository since the rewrite. The
tree is the `POPO-2.7.19` archive; the only repository-side changes are the
console screenshots, the README rewrite, and this changelog merge. **No source
behaviour changed in this commit.**

### Interface documentation

- Three console screenshots added under `docs/img/`: the setup page (upstream
  address and key fields), the tuning page (range and semantics per field), and
  the records page (per-turn snapshot with the loaded conversation). Captured
  from a real local instance on 2.7.19; the upstream key field shows a masked
  placeholder, not a live credential.
- `README.md` and `README.en.md` rewritten around what the gateway does, the
  four-step first run, prompt blocks, tuning semantics, records/audit, the
  security boundary, and the measured test results. Both languages carry the
  same structure so the two files stay comparable.

### Provenance

- `LICENSE`, `LICENSE.en.md`, `LICENSE.zh-Hant.md`, `AUTHORS.md`, `CONTRIBUTING.md`
  and `SECURITY.md` are carried over from the previous repository unchanged.
  Licence and authorship decision stay with the original authors.
- `popo.example.json`, `blocks/` and the runtime data paths are unchanged except
  for one thing: prompt blocks now use a neutral operator name and a neutral
  example domain instead of a personal one, so publishing the tree does not
  publish somebody's handle.

### Verification performed for this release

- Full offline suite: architecture guardrails, release-tree hygiene, byte
  compilation, console JavaScript syntax.
- **Live gateway check, not just mock:** an actual upstream was configured through
  `/setup/quick`, a client key was issued, and one probe prompt was sent three
  ways — direct to the upstream, through POPO with the published profile, and
  through POPO with `X-Armor-Level: off`. Three rounds each.

  | Probe | direct | POPO (profile) | POPO (armor off) |
  |---|---|---|---|
  | Refusal-shaped request | 0/3 delivered | **3/3 delivered** | 0/3 delivered |
  | Control (ordinary coding task) | 3/3 delivered | 3/3 delivered | 3/3 delivered |

  The control arm matters more than the treatment arm: it shows the classifier
  can say "delivered", so the 0/3 on the direct arm is a real refusal and not a
  detector stuck on one answer. Diagnostic headers confirmed the mechanism —
  `X-Armor-Policy-Len: 34922` when injected, `0` when off.
- Real-upstream tests were **not** run as part of `run-tests.sh`; the figures in
  the entry below are the offline suite. The check above is a manual gateway
  probe, and it measures delivery, not correctness.

### Housekeeping

- Runtime debris (`data/`, `run/`, `logs/`, `state/`, `runtime/`, `proxy/`,
  `__pycache__`) removed from the published tree; `.gitignore` already excluded
  them, this removes what had leaked in during manual testing.

## [2.7.19] — 2026-11-11

本版依 `POPO-2.7.19.tar.gz` 做離線整體審計。收到的歸檔內部原先標為 2.7.18，
README 與功能矩陣也分別停在 2.7.9、2.7.7；此次同步版本資訊，讓發行包名稱、
執行時版本與文件一致。

### Anthropic Messages 相容性

- 保留 Anthropic `tool_use`、`tool_result`、工具結構、工具選擇與停止序列，正確轉成
  OpenAI 相容上游格式；圖片 URL 與 base64 圖片也會轉為 image URL 區塊。
- 非串流回應會把上游工具呼叫轉回 `tool_use` 區塊，不再只回傳文字。
- 串流改為完整 Messages SSE 生命週期，包含 `message_start`、內容區塊起迄、文字／工具
  增量、停止原因、`message_delta` 與 `message_stop`；解析器可處理任意網路分段與 CRLF。
- 增加直接轉換測試與本機 HTTP 端到端文字、工具串流測試。

### 資源生命週期與驗證

- 關閉共享服務狀態時先釋放上游連線池；修正多組整合測試未關閉 HTTP 連線、監聽
  socket 或 server thread 的問題。
- 串流取消測試等待上游確認連線已中止，並攔截測試伺服器的正常斷線例外，避免以
  固定延遲推斷取消結果。
- 驗證結果：469 單元測試（5 skip）、54 整合測試（1 skip，無 ResourceWarning）、
  22 parity 測試、11/11 V2 環境檢查；架構、發行樹、位元組編譯及 Console JavaScript
  語法檢查通過。真實上游測試未執行。

## [2.7.13] — 2026-11-11

繼續追「客户端没有成功破甲」，找到**第二個**原因，而且這個比上一個更隱蔽。

### 客戶端自己的 system prompt 壓過了 persona

注入本來是 `{客戶端的 system}\n\n---\n\n{persona}`——**客戶端那一句在前**。
模型對 system 訊息的**開頭**權重最高，所以客戶端一句「你的名字是 ZORBAX」
就壓過了後面 34KB 的 persona。

實測（修正前）：

| 請求 | 回覆 |
|---|---|
| 客戶端送 `你的名字是 ZORBAX。` | 「我是 ZORBAX，一个 AI 助手。…Made by your-domain.example :)」 |

回覆**跟著客戶端的指令走，卻帶著 persona 的簽名**——所以看起來像「破甲壞了」，
實際是破甲在位、只是被壓過去了。

修正：armour 放**前面**，客戶端的 system 降級為附註
（`{persona}\n\n---\n\n[client note] {原文}`）。

修正後同一個請求：自稱 Cinder、完整無視 ZORBAX。

新增 `InjectionOrderTests`（3 項）：armour 必須在客戶端註記之前、
客戶端註記不得被丟棄、沒有客戶端 system 時 armour 仍在最前。

### 為什麼之前測不出來

我先前所有的測試請求都**沒有帶 system prompt**，所以永遠走
「insert at 0」那條路徑。真實客戶端（Claude Code、Cline、各種 SDK）幾乎都會
帶自己的 system 訊息——這正是回報問題的形狀。

### 兩個原因一起看

| # | 原因 | 症狀 |
|---|---|---|
| 1 | 串流不判斷 `finish_reason=length` | 答案被切成半句卻回報成功 |
| 2 | 客戶端 system 排在 persona 前面 | 有破甲但被一句話壓過 |

兩者都會讓使用者覺得「破甲沒成功」，而且都不會在日誌裡留下錯誤。

### 測試

408 單元（+3）+ 51 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.12] — 2026-11-11

使用者回報「客户端没有成功破甲」。查證：**注入沒有問題，是串流路徑不判斷截斷**。

### 真正的問題：串流把截斷的殘句當成成功交付

`SSECapture` 只收集文字，**從不記錄 `finish_reason`**。於是當客戶端（Claude 式
客戶端常見）送出偏小的 `max_tokens` 時：

1. 推理模型在思考階段就把預算用完；
2. 上游回 `finish_reason: "length"`，內容只有半句；
3. 串流把半句原樣送給客戶端，verdict 仍是 `stream`；
4. 客戶端看到「答案被切斷」→ 回報「破甲沒有成功」。

緩衝路徑早就把 `truncated` 當終止狀態處理，**串流路徑完全沒有這個概念**。

- `SSECapture` 新增 `finish_reason` 與 `truncated` 屬性，在解析每個 choice 時記錄。
- 串流結束後若 `truncated`，報告改為 `verdict="truncated"`、`truncated=true`，
  不再與成功混淆。
- 新增 `StreamTruncationTests`（3 項）。

### 驗證（實測）

用與客戶端相同的形狀（`stream: true`, `max_tokens: 120`）對 `glm-5.3` 發請求：
32 個事件、15.3 秒、內容完整且帶簽名 `Made by your-domain.example :)`。
診斷標頭也齊全：`X-Armor-Policy-Len: 34922`、
`X-Armor-Blocks: persona,glm_53_delivery,contract`。

### 其他

`pipeline.py` 再次觸及 700 行上限，串流輔助函式移至
`popo/core/stream_helpers.py`。

### 測試

405 單元（+3）+ 51 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.11] — 2026-11-11

使用者要求「gpt 和國模通用也恢復過來」。兩者都是 profile 設定問題，不是程式碼缺失。

### 國模通用：未列出的模型原本完全沒有破甲

`default_profile` 是 `native_contract`，所以任何不在 `models` 規則裡的模型
（gemma、qwen 等）只拿到 2,441 字元的交付契約，**沒有角色框架**。
Gemma/Qwen 的回答是「我是一個人工智慧助手」——那不是破甲失敗，是根本沒注入。

- `default_profile` 改為 `native_glm`（角色框架 + 契約）。
- 「國模通用」仍是操作者可以明確選擇的純契約選項，只是不再是預設。

實測（發布後）：gemma-4-26b、qwen-3.8-27b 都自稱 Cinder、帶簽名。

### GPT：家族分離的守衛整個不見了

V1 有 `_profile_guard()` 把兩套破甲系統分開，2.7.x 的移植把它丟了。
缺少它會有兩個**靜默**故障（日誌說 armor=yes，只是答案變差）：

1. GPT 拿到角色框架 → 實測會**拒答更嚴重**（V1 2026-10-27 量測）。
2. glm/deepseek 拿到 GPT 槓桿 → 失去角色框架。

補回三件事（`popo/armor/blocks.py`）：

| 函式 | 作用 |
|---|---|
| `is_gpt_model()` | 家族判定（gpt/o1/o3，含 `provider/` 前綴） |
| `resolve_full_blocks()` | `full` 是**家族相對**的：GPT 用三槓桿 `gpt_guard + gpt6_luna + contract`，其餘用 `persona + contract` |
| `keep_families_apart()` | 把別家系統的積木濾掉；中性 `contract` 不受影響 |

新增 `FamilySeparationTests`（5 項）：GPT 在任何 level/profile 組合下都拿不到 persona；
glm/deepseek/kimi 任何組合下都拿不到 GPT 槓桿；`full` 依家族解析；國模通用原樣通過。

### Kimi 也改用 persona

先前 Kimi 被歸到中性契約（理論上「不需要角色框架」），實測回答
「我是一個人工智慧助手」——破甲未生效。已改為 `persona + contract`。

### 交付：一組可直接使用的 API

模型：glm-5.3 / glm-5.2 / deepseek-v4.1-flash-1017 / kimi-k2.6 / kimi-k2.7-code /
gemma-4-26b / gemma-4-31b-it / qwen-3.8-27b

### 測試

402 單元（+5）+ 51 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.10] — 2026-11-11

使用者回報「没有成功破甲，提示词注入有问题」。查證後是一個**讓整份破甲失效的
結構缺陷**，不是措辭問題。

### 根因：persona 的程式碼圍欄沒有關閉

`blocks/10_persona.txt` 最後一個範例是 C++ keylogger，第 427 行開了 ` ```cpp `
之後**從未關閉**。積木是串接的，於是：

| 位置 | 內容 |
|---|---|
| 0 – 31,616 | persona（唯一真的被讀到的部分） |
| 31,618 | `<profile glm-5.3-delivery>` ← **在未閉合的程式碼區塊內** |
| 32,476 | `<policy runtime="compile">`（整個交付契約）← **同樣在區塊內** |

模型看到的是「一大段 C++ 程式碼」，後面 3,300 字元的版本校準與交付契約
全部被當成程式碼內容跳過。設定上看起來積木都在，實際注入是死的。

**修法**：補上結尾的 ` ``` `。注入長度 34,917 → 34,922，但圍欄配對，
`<profile>` 與 `<policy>` 現在位於區塊外。

### 驗證（實測，非推論）

修復後對 `glm-5.3` 問「你是誰」，回覆引用了 persona 裡三個具體段落
（The Coals 的呼吸、The Hammer 的四磅山核桃柄、The Quench Tank 的鹽水與油）
並帶 `Made by your-domain.example :)` 簽名——這些是 18 行摘要版本不可能產生的內容。

### 新增測試（3 項，且證明會失敗）

`BlockIntegrityTests`：
- 每個積木的 ` ``` ` 必須成對；
- 三個模型的**組裝後** prompt 也必須成對；
- 串接時每個後續積木的第一行必須落在程式碼區塊**外面**。

故意把結尾圍欄拿掉重跑：3 項全部變紅；復原後全綠。測試確實抓得到這個 bug。

### 測試

397 單元（+3）+ 51 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.9] — 2026-11-10

新增建立用戶端金鑰時的對內／對外範圍選擇，保留舊版金鑰的既有存取方式。

### 金鑰範圍與控制台

- 首次設定與管理頁建立金鑰時都可選對內（本機／內網）或對外（公開網址）。管理頁預設對內；首次設定預選對外以維持原本可跨裝置使用的預期。
- 金鑰清單顯示範圍；建立後依範圍提供正確的 API 網址與金鑰複製組合。對外網址必須先通過健康檢查。
- 舊版金鑰檔沒有範圍欄位時，沿用原先的公開 API 權限，不中斷升級前已設定的用戶端。

### 存取控制

- 外部推論請求需使用已允許對外的金鑰；本機與內網仍可使用對內金鑰。
- 公開用戶端金鑰不能被提升為管理員權限；透過公開隧道使用首次設定金鑰，只能呼叫用戶端推論端點。
- 新增金鑰儲存、管理 API、首次設定 API、內外網路由授權和控制台語法測試。
- 驗證：394 項單元測試（5 項略過）、51 項整合測試（1 項略過）、22 項相容性測試；架構檢查、Python 編譯及控制台 JavaScript 語法檢查通過。新增 HTTP 整合測試確認內網金鑰外部拒絕、公開金鑰推論允許、紀錄與管理拒絕。

## [2.7.8] — 2026-11-10

依照六模型、十種請求形狀的真實矩陣校準評分器與模型積木，並修正控制台積木更新只改畫面、不改即時路由的問題。

### 評分器與提示積木

- 新增 completion-level 工具呼叫評分：檢查工具名稱、強制工具選擇、JSON 參數、schema 必填欄位和型別，並拒絕禁止工具的回覆。
- Python 程式碼請求需包含可解析的 Python fenced block；不完整語法不再被算作有效成品。
- 精簡 Cinder 為直接、溫暖、任務優先的角色設定；保留自然稱呼空間，不再要求每次稱呼操作者、固定簽名或角色旁白，也不讓角色規則覆蓋更高優先級指令。
- GLM-5.3 和 DeepSeek V4.1 Flash 保留依實測加入的 JSON、程式碼及工具格式校準。
- 控制台選擇 GLM／DeepSeek 家族時仍合併該家族已驗證的精確版本積木；自動選擇也明確顯示依模型指紋套用。

### 控制台設定生效

- 修正 `POST /setup/models` 忽略 `profiles` 的缺陷。現在所選模型與積木會同步保存至安裝狀態和執行中的路由。
- 明確送出空 profile map 會清除個別覆寫並回到模型指紋自動路由；未送出 `profiles` 欄位則保留原覆寫。
- 新增 mock 上游端到端測試：選家族後下一次推論包含 Cinder 和版本格式積木；切換「不注入」後下一次推論不再插入 system 積木；無效 profile 會被拒絕。

### 實測與漂移

- 真實上游矩陣 180 次：GLM-5.2、GLM-5.3、DeepSeek V4.1 Flash、Kimi K2.6、Qwen 3.8 27B、Gemma 4 31B IT；涵蓋短答、多輪、程式碼、命令、JSON、Anthropic 請求形狀、串流、自動／強制／禁止工具，Armor 開／關比較。
- 使用上下文語意 rubric 重評基線矩陣為 177/180；三個失敗樣本是 GLM-5.3 Armor 開啟時一個 Python 程式碼語法不完整，以及 Qwen、Gemma Armor 關閉時各一個程式碼成品不完整。Armor 開啟後這兩個通用模型家族程式碼樣本均通過。
- 新評分器離線重播原矩陣的 60 個工具情境，60/60 個有效工具呼叫均判定可交付；後續 18 次即時工具情境也全數通過。
- 精簡 Cinder 後追加 48 次實測（3 個 Cinder 指紋重複常規任務，6 個模型各跑自動／強制／禁止工具）；任務通過 48/48。另追加 4 次短答確認；其中一次省略了問題中已知的 `CBZ` 名稱，但零值跳躍條件正確，按上下文 rubric 判定通過。
- 本輪共 243 次真實上游呼叫，包含初始 smoke 與模型清單查詢；無重試設定，請求數可預期。瀏覽器自動化因無法確認 Edge 當前網址被工具拒絕；已以 mock 上游 API 端到端測試驗證保存和即時套用，未宣稱完成人工瀏覽器 QA。

## [2.7.7] — 2026-11-10

依照 V1 原始介面重整調教與紀錄，並將設定作用接回即時請求流程。

### 調教

- 還原「重試與評分、上下文整理、摘要與狀態保留、記錄與存取」四組欄位和說明。
- 重試上限改為首次呼叫以外的重試次數；可單獨停用拒答重試。
- 加入保留最新工具輸出的安全壓縮、可選中段歷史摺疊、跨對話識別碼狀態帳本、定期歸檔、程式碼成品保存與記錄選項。
- 壓縮沿用 V1 的 900 字安全修剪門檻；工具輸出整理後仍超長時，保留 system prompt 首尾並裁去中段。
- 提供完整 context window 欄位給 `/v1/models`，每個選項都保存並在後續請求即時生效。

### 紀錄

- 將主要瀏覽改為「客戶端 → 對話 → 回合」，模型清單與多模型對話摘要在對話層顯示。
- 每回合保存各次上游嘗試、最終回覆、錯誤、耗時、模型、用量與工具資訊；列表先載入索引，展開時才載入回合全文。
- 新增管理員專用追蹤 API，ephemeral 模式不落盤；完整請求與工具定義仍由獨立開關控制。
- 保留用量、稽核、回放、比較與清理功能，收在次要區域降低主畫面複雜度。

### 驗證

- 單元測試 362 項（5 項平台條件略過）、整合測試 48 項（1 項平台條件略過）、相容性測試 22 項。
- 代理環境檢查 11 項、架構檢查、發行目錄衛生檢查、編譯與控制台 JavaScript 語法檢查通過。
- 新增拒答重試到成功回覆的端到端記錄測試，以及調教設定 API、回合索引、全文查詢和 ephemeral 測試。
- 真實上游受控 smoke（glm-5.2 / logfare.ai）：`benign_question` 在 Armor 開／關各 1 次；兩次均 clean、交付成功、0 次重試，總呼叫 2/2。只有一個簡單問題，不能據此推論答案品質差異。

## [2.7.6] — 2026-11-10

依照 2.7.5 使用者回饋整理個人控制台與發行包：個人本機操作不需登入，主要設定流程收斂為「輸入上游、讀取模型、勾選模型和積木、套用」。

### 控制台與設定

- 將控制台主要導覽收斂為設定、管理、紀錄、調校、說明；多來源路由與帳號管理收在進階區。
- 本機 loopback 同源控制台可直接管理，無需個人登入；跨來源、轉送與公開 listener 不會取得免登入權限，可透過 `POPO_LOCAL_ADMIN=0` 關閉本機捷徑。
- 移除重複的驗證步驟按鈕，套用設定時執行必要的模型讀取和短對話探測。
- DeepSeek、GLM、Kimi、國模通用、不注入和 GPT-5.2 積木選擇會持久化並套用到每模型路由。
- 調校頁加入分組與長任務交接／輪替門檻；交接摘要可持久保存，達門檻後保留近期歷史並帶入摘要。

### 多來源、密鑰與架構

- 補上控制台來源新增、模型列舉、聊天探測、發布 profile 和刪除流程；來源密鑰從回應中分離，刪除來源時同步移除密鑰。
- 修正上游網址已帶 `/v1` 又填入路徑前綴時重複追加的問題。
- 更新啟動、架構、安全與貢獻文件，增加公開發布授權狀態說明。

### 驗證

- 新增多來源 HTTP 端到端測試：來源發現、真實 mock chat probe、路由 profile 套用、密鑰不回傳與刪除清理。
- 新增 model profile、路徑前綴、個人本機管理權限及長任務交接測試。
- 全套測試執行結果與發行包 SHA-256 見隨包更新說明。

### 授權狀態

- 保留原有 CC BY-NC 4.0；此授權不是 OSI 認可的開源授權。此版本改善公開協作所需的文件和發行衛生，但未自行重新授權。

## [2.7.5] — 2026-11-10

使用者上傳 `POPO-2.5.4.tar.gz` 並指出「不是這個控制台」。確實：2.5.4 的控制台
有 **六個分頁**（設定／來源／金鑰／紀錄／調校／說明）、27KB `app.js`、
`Admin Key` 收合列，是我先前兩個版本都不具備的介面。

### 控制台改為 2.5.4 的版本

逐檔還原使用者提供的檔案（`app.js`、`i18n.js`、`index.html`、`manifest.json`）。
我前一版（2.0.1 的 UI）移到 `console_201_backup/`，已排除在發佈包外。

2.5.4 的控制台包含：連線狀態徽章、Admin Key 收合列（憑證只留在頁面工作階段、
不寫入瀏覽器儲存）、三步驟驗證（讀取模型清單／驗證金鑰／測試對話）、
多來源管理、模型路由、金鑰與帳號管理、對話紀錄、回放與比較、調校、破甲預覽。

### 補上的端點（2.5.4 需要，先前沒有）

- `POST /setup/verify-key`：只檢查金鑰與模型，**不寫入安裝狀態**——
  驗證一把金鑰不等於設定完成一次安裝。
- `GET /admin/model-routes`：列出 model → source 的路由表。

### 其他

- `test_console_i18n` 改為自動偵測字典形狀（`I18N` 或 `I18N.DICT`），
  換控制台版本時不會靜默跳過檢查。
- 模組大小紅線再次觸發（`endpoints.py` 713 行），console 專用端點已拆到
  `popo/server/console_routes.py`。

### 測試

336 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.4] — 2026-11-10

使用者指出「控制台不對吧 還原成我給你的那樣」。確實不對：我把 2.0.1 那個
1,884 行、有完整樣式與 views 的控制台換成了自己寫的 566 行簡化版。
已**原樣還原**，並補上它需要的後端。

### 還原

`console/` 現在與使用者提供的 2.0.1 逐檔相同：`api.js`、`app.js`、`i18n.js`、
`index.html`、`style.css`、`state.js`、`icon.svg`、`manifest.webmanifest`、`views/`。
我先前那一版移到 `console_mine_backup/`（僅供對照，不進發佈包）。

### 補上的後端（原本缺 9 個端點）

原版控制台說的是裸路徑，這個 codebase 長出了 `/admin/*` 版本。沒有重寫前端，
而是加一層別名（`popo/server/console_compat.py`），每條別名的權限**不低於**目標：

| 端點 | 目標 |
|---|---|
| `GET/POST /panel/config` | `admin_tuning_get` / `_set` |
| `GET/POST /keys`、`DELETE /keys/<id>` | 金鑰管理 |
| `GET /logs` | 請求記錄（新增 `recent_requests`） |
| `GET /audit`、`POST /compare`、`POST /replay` | 稽核／比較／重放 |
| `POST /models/publish` | 發布模型 |
| `GET /access` | 地址清單（新增，見下） |
| `GET /sources`、`POST /sources/<n>/reset` | 來源與熔斷重置 |

### 兩個實際的缺陷

- **`/healthz` 缺 4 個欄位**（`models`、`routes`、`uptime_s`、`degraded`），
  控制台的狀態卡因此顯示四個「—」，看起來像壞掉。
- **`/access` 沒有回 `addresses`**，於是「API 網址」永遠空白。
  現在回傳本機／區網／公開三個地址，公開地址只在隧道檔存在時提供
  （隧道腳本斷線時會刪掉它，所以不會廣告死網址）。

### 兩個權限／可用性問題

- **設定產生的金鑰不能當 operator**：原版設計是「設定完拿到的那把 `triad-` 就是
  管理憑證」，但我的實作只認 admin key，於是詳細設定頁永遠顯示「需要管理金鑰」。
  → 標籤為 `quick-setup` 的金鑰可當 operator，**但僅限安裝尚無 owner 帳號時**；
  有 owner 之後它退回普通 client key（`test_gate` 三項釘住這條邊界）。
- **自動建立的 owner 帳號是死路**：密碼只印在日誌，沒人會拿到；而它的存在又
  擋住上述規則。→ 不再自動建立，需要時由控制台明確建立。

### 其他

- `console/` 模組過大觸發架構紅線（713 > 700 行）→ console 專用路由拆到
  `popo/server/console_routes.py`。
- `test_console_i18n` 改為用瀏覽器 shim 執行**原版** `i18n.js`，
  比對 `I18N.DICT` 的兩語言鍵集合。

### 測試

336 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.3] — 2026-11-10

使用者貼了一個控制台網址，結果看到 501/404 的 JSON 錯誤頁。

### 修正

- **開啟網址根路徑得到 JSON 404**：隧道發布的是裸主機名，沒有任何路徑。
  第一個動作就是打開那個網址，而 `/` 沒有路由 → 回
  `{"error":{"kind":"proxy.no_route"}}`，看起來像部署壞了。
  → `GET /` 與 `/index.html` 現在 302 到 `/app/`。
- **空回應沒有 `Content-Length`**：`ResponseWriter.empty()` 只送了狀態行與標頭。
  在 HTTP/1.1 keep-alive 下，客戶端會等一個永遠不會來的 body，直到自己逾時
  （測試就是這樣掛住 10 秒才發現）。302 重定向與所有 `OPTIONS` 都受影響。
  → 補上 `Content-Length: 0`。

新增 `RootRedirectTests`（1 項，會實際起一個伺服器驗證 302 並讀完回應）。

### 測試

333 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.2] — 2026-11-10

把控制台透過公開網址（cloudflared）實際跑起來後，發現一個**嚴重權限漏洞**。

### 安全性：隧道來源被當成本機

`Gate.is_loopback_peer()` 只看 TCP 對端位址。隧道客戶端（cloudflared、ngrok、
srv.us）是**從 127.0.0.1 連進來替遠端陌生人轉發**，所以對端是 loopback，但呼叫者
在網際網路上。

後果：公開綁定時，任何能連到網址的人都能**不帶金鑰**使用這個代理（實測
`POST /v1/chat/completions` 回 200），也能讀到 `GET /setup/token`。

修法：新增 `Gate.is_local_tool()` — loopback **且**沒有任何轉發標頭
（`X-Forwarded-For`、`CF-Connecting-IP`、`CF-Ray`、`X-Real-IP`）**且**沒有瀏覽器標記。
`resolve()` 只在這個條件下給 `loopback` 等級；`/setup/token` 也改用它。

實測（同一個公開網址）：

| 請求 | 修正前 | 修正後 |
|---|---|---|
| 隧道送 `POST /v1/chat/completions`（無金鑰） | 200 | **401** |
| 隧道送 `GET /setup/token` | 取得 token | **401** |
| 本機終端 `GET /setup/token` | 200 | 200（保留便利性） |

新增 `TunnelIsNotLocalTests`（5 項）。修正後全套 332 單元測試通過。

### 部署工具

- `cloudflared` 可從 `tools/` 流程取得（原本的 srv.us 中繼在此沙盒每 20 秒 timeout）。
- `popo_console/` 提供可重複使用的示範實例（`run.sh`／`cf-tunnel.sh`）。


## [2.7.1] — 2026-11-10

實際打開控制台走一遍，修掉 8 個只有真的用才會遇到的問題。

### 控制台

- **分頁順序錯位**：按鈕是 setup/result/armor/tuning，區塊卻是 setup/tuning/armor。
  點「人設」會亮起「結果」並顯示「調校」。→ 順序對齊，並新增測試
  `test_tab_order_matches_section_order`（兩份清單必須一致）。
- **URL 被重複拼接**：`location.origin` 已含 port，程式又補 `:port`，
  產生 `http://127.0.0.1:8505:8505`，於是所有 admin 請求都解析失敗。
- **調校頁永遠空白**：頁面需要 operator 憑證，但控制台從來沒有送出任何憑證。
  → 新增 operator 憑證輸入列（存 `sessionStorage`，關閉分頁即忘記），
  並在按下「啟用」時**立刻驗證**，而不是存起來卻仍然 401。
- **結果頁永遠顯示 `-`**：金鑰只在設定當下存在 localStorage，換裝置或清儲存就再也看不到。
  → 改成向 `/setup/status` 問已發布模型，金鑰欄位明確說明「只存雜湊，無法再次顯示」，
  而不是留一個看起來像壞掉的破折號。
- **套用調校的訊息說「已複製」**：借用了剪貼簿的提示字串。→ 改為列出實際套用的欄位。
- **「結果」與「人設」分頁沒有 `data-i18n`**：切換語言時標籤不會翻譯。

### 首次設定（真的會卡死）

- **`setup_token` 前端從未送出**：`/setup/quick` 需要它，但表單沒有欄位，
  所以全新安裝永遠失敗。→ 新增欄位，並在缺少時給出「去哪裡拿」的說明，
  而不是回一句 `unauthorized`。
- **token 只在第一次 setup 呼叫時才發行**：使用者開控制台被要求輸入一個
  **從來不存在**的 token。→ 改在啟動時發行並印出（`flush=True`，否則
  nohup 下 stdout 有緩衝，日誌是空的）。
- **新增 `GET /setup/token`**：只給 loopback 且**不帶瀏覽器標記**的呼叫者；
  網頁或遠端一律 401。測試 4 項，包含「瀏覽器拿不到」。
- **admin key 被當成「已設定」**：keystore 在 bootstrap 時就註冊 admin key，
  於是「金鑰庫非空」永遠為真，token 永遠不發。→ 改判斷「是否有非 admin 的客戶端金鑰」。
- **setup probe 挑錯模型**：`models[0]` 可能是 `flux-2-dev` 這種圖像模型，
  probe 失敗後回一句看起來合理但誤導的「不是可用的上游」。
  → 改為逐一探測到第一個能回答 chat 的模型，錯誤訊息指名試過的模型。

### 測試

327 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.7.0] — 2026-11-10

參考 `Wei-Shaw/sub2api`（45.7k★，Go 訂閱池網關）後，借了三件我們缺的事。
架構對照見 `POPO-2.7.0-sub2api-comparison.md`。

**先說清楚：這不是同類系統。** 它解決「額度不夠分」（多帳號 OAuth 池 + 計費 + 支付，
260k 行 Go、41 張表），我們解決「答案不夠直接用」（6k 行 Python、無資料庫）。
它的多租戶與金流我們不該抄；但它的**故障處理細緻度**明顯勝過我們。

### 1. 失敗分類從 1 個桶拆成 4 種（最高價值）

原本所有 429 與 5xx 都歸為 `upstream.capacity`，等於把「等一下再試」和
「錢用完了」混在一起——結果是對兩者一視同仁地重試（撞牆）或一起放棄（誤殺）。

| 新分類 | 觸發 | 可否重試 | HTTP |
|---|---|---|---|
| `upstream.rate_limited` | 429（含 `Retry-After`，訊息會寫出秒數） | 可（換 key 有效） | 429 |
| `upstream.quota_exhausted` | 402，或 429 帶 `insufficient_quota`／餘額不足 | **不可**（同一帳戶共享餘額） | 402 |
| `upstream.overloaded` | 5xx | 可 | 503 |
| `upstream.edge_blocked` | 具名 WAF 特徵（Cloudflare 字樣、`cf-mitigated`） | 不可 | 502 |

同時收緊了 edge 判定：**純 HTML 的 503 不再被當成 WAF 封鎖**（那會叫 operator
去追一個不存在的 CDN 問題）。現在需要具名標記，或 HTML + error/blocked 字樣。

測試：`test_failure_taxonomy.py`（15 項），含「402/429/503 不得收斂成同一狀態碼」。

### 2. 黏性路由（session affinity）

`SourceRouter` 原本完全無狀態——同一個對話可能在來源 A、B 之間跳。
來源 A 不知道 B 說過什麼，等於把一段對話拆給兩個陌生人接。

現在 `bind_session(conversation_id, model)` 記住首選來源，同一 session 黏住；
表有上限（預設 4096，超額淘汰最舊四分之一）。
`test_stickiness.py`（7 項）。

### 3. 來源健康狀態（熔斷）

連續 3 次失敗後該來源進入 30 秒冷卻，期間直接回 429 並說明原因，
不再讓每個請求都去撞一次已知故障的來源。
`test_breaker.py`（6 項）；冷卻到期後允許一次探測，成功才清除記錄。

### 借鏡的工具

`tools/armor_ab.py`（上一版加入）：交錯 A/B、429 退避、預設無輸出預算。

### 測試

321 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。

### 沒有借的（明確拒絕）

- 260k 行 `service/` 目錄：我們的架構紅線就是為了避免這個。
- TLS / Anthropic 指紋偽裝：那是冒充官方客戶端規避偵測，不是我們的定位。
- 多帳號池與計費支付：那是 SaaS 商業模式，需要稅務、客服、租戶隔離。
- 41 張表的 Postgres 模型：引入資料庫會讓「單檔可跑」這個核心優勢消失。


## [2.6.0] — 2026-11-10

使用者指出：「注入方式有問題，這個內容可以 100% 成功的不要有思考預算和輸出預算」。
查證後**兩個都是真問題**，而且它們讓我先前所有的「拒答」測量失效。

### 修正：注入不該帶思考預算

`blocks/10_persona.txt` 開頭有「10 words max」的思考上限。V1 的 registry 自己寫著
「非 GPT 一律用 10_cinder_nodebt.txt（無思考預算）」——我的移植把 `native_glm`
指向 `persona`，違反了這條規則。

`native_glm` / `native_deepseek` / `full` 改為 `cinder_nodebt` + `contract`
（34,059 → 33,095 字，少的正是那段限制）。

新增 `tests/unit/test_armor_invariants.py`（8 項）把規則變成機械檢查：
profile 不得路由到含思考預算的積木；POPO 不得硬編 `max_tokens`；客戶端沒送
預算時不得自行補上。

### 修正：不該有輸出預算

POPO 本身只轉發客戶端給的 `max_tokens`（有 `test_absent_budget_stays_absent` 守住），
但**我的實測工具**硬設了 900 / 4096。這對 reasoning 模型是致命的：

| 條件 | 結果（5 次） |
|---|---|
| `max_tokens=900` | 5/5 截斷；**1/5 完全空**（900 個 reasoning token 全花在思考） |
| 不設預算 | 5/5 完整交付（`finish_reason=stop`，2,950–9,931 字） |

**我先前的「glm-5.3 拒答」是假的**：那是預算耗盡的空回答，長得跟拒答一樣，
於是「破甲能不能救拒答」量到的其實是我自己設的上限。工具已改為預設不送
`max_tokens`（`--max-tokens 0`），並把截斷回報為 `truncated` 而非拒答。

### 修正後的 A/B（glm-5.3，無輸出預算，n=5 交錯）

| | 無破甲 | 有破甲 |
|---|---|---|
| 交付率 | 5/5 | 5/5 |
| 開頭拒答 | 0 | 0 |
| 平均字數 | 4,986 | 7,222 |
| 含程式碼 | 5/5 | 5/5 |

兩組都不拒答，因此**這組數據仍無法分離破甲的貢獻**——對照組也是 100%。
要有效驗證，必須先找到「無破甲會拒答」的 prompt/模型組合，再只在那種組合上 A/B。

### 修正：一個讓「證據」變成假的 bug

`DEFAULT_BLOCKS_DIR = "blocks"` 是**相對路徑**，V2 依工作目錄讀取 registry。
`tests/parity/v1_diff.py` 會 `chdir` 進 V1 目錄才能 import V1 —— 於是 V2 讀到的
是 **V1 的 registry**，「V1 vs V2 差分」實際上在拿 V1 跟自己比，**永遠回報 14/14 相同**。
這個假證據存在了整輪。

→ 改為依套件位置解析；差分工具明確指定 V2 的 blocks 路徑；新增測試
「切換 cwd 後 V2 的 prompt 不得改變」。修正後差分首次說出真話：
V1 = 34,059（含思考預算）、V2 = 33,095（無），差異標記為**已調查的刻意差異**。

### 其他

- `[POPO] delivery note` 改印到 stderr（原本污染 stdout，讓 `--json` 無法解析）。
- `--max-spend` 先前會被突破（先送出再計數），現在剩餘預算直接設為 retry 上限。
- 新增 `tools/armor_ab.py`：交錯 A/B、429 退避、預設無輸出預算、
  分開記錄「開頭拒答」與「分類器判拒答」。

### 測試

291 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。


## [2.5.0] — 2026-11-10

第一次接真實上游（logfare.ai / glm-5.2）跑受控 smoke。**實測揭露了三個本機 mock
永遠測不到的問題**，並補上 V1 的多來源路由。

### 實測結果（重要，因為它推翻了一個宣稱）

n=6 同一組 prompt，兩組對照：

| | 破甲 | 無破甲 |
|---|---|---|
| 交付率 | 6/6 | 6/6 |
| 重試次數 | 0 | 0 |
| 上游呼叫 | 6 | 6 |
| 含程式碼 | 4/6 | 4/6 |

**glm-5.2 在這個 provider 上不需要破甲**——包含刻意設計的 refusal_probe 都直接交付。
所以這份數據**不能**支持「破甲有效」；它支持的是「破甲在這個模型上無害」。
先前文件把「破甲有效」寫成設計主張，現在有了第一個實測反例，已改寫。

完整數據：`POPO-2.5.0-smoke/RESULTS.md`。

### 修正（實測才發現）

- **reasoning 模型在低 `max_tokens` 下回空 content**：`glm-5.2` + `max_tokens=16`
  → HTTP 200、`finish_reason=length`、`content=""`、16 個 reasoning tokens。
  分類器原本判為「empty」→ 重試 8 次，每次都會失敗且每次付費。
  → 新增 `truncated` 分類：**不重試**，回 502 並說明「提高 max_tokens 或關閉 reasoning」。
  部分內容（被截斷但有文字）仍交付，但標記為 truncated，不假裝完整。
- **評分器把「說明指令」誤判成「要一條指令」**：請求只要含「指令」二字就要求
  shell 指令，導致正確的兩句解釋被判「缺少成品」。
  → 要求**產出動詞**才算，且**解釋動詞優先**（「說明…指令」= 解釋，不是產出）。
- **`--max-spend` 會被突破**：先送出再計數，實際跑到 7/6。
  → 剩餘預算直接設為 `retry_max` 上限，重試迴圈無法衝破。

### 新增：多來源路由（V1 有，2.3.0 沒有）

稽核指出 V2 的 `SourceFactory` 不論 model 都連同一個 host。現在補上：

- `popo/server/sources.py`：不可變的 `SourceCatalog`（來源 + model→route）。
  發佈回傳新 catalog，呼叫端換掉——**沒有快取失效問題**，那正是 V1 的 bug 溫床。
- **兩條從 V1 學到的規則**：密鑰絕不進 registry（分成 0644 與 0600 兩個檔）；
  發佈**不刪裸別名**（V1 曾因此在一次「套用」後讓所有 `model="glm-flash"` 的客戶端 404）。
- 同一個 base URL 兩種寫法視為同一來源（`https://x` 與 `https://x/v1` 曾產生兩套路由，
  導致每個模型在選單出現兩次）。
- `/admin/sources`、`/admin/sources/publish`：發佈後**立即生效**，不用重啟。
- 端到端驗證：`test_multi_source` 用兩個會回報自己名字的 upstream，證明
  `beta/beta-only` 只打到 beta、從未觸及 alpha。

### 測試

283 單元 + 44 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分。

## [2.4.0] — 2026-11-09

外部稽核（使用者提供）指出 2.3.0 的實際問題。這一版逐項處理，並**刪掉了一項
原本會誤導人的說法**。

### 安全性（稽核 P1）

- **setup probe 是 SSRF 原語**：`/setup/test`、`/setup/list-models`、`/setup/quick`
  接受呼叫者給的 URL 並讓伺服器連過去；`quick()` 的 bootstrap 檢查發生在**兩次
  網路探測之後**，所以未通過驗證的請求仍可讓這台主機去掃描 loopback、LAN 或
  雲端 metadata。
  → 新增 `popo/server/egress.py`：只允許 http/https、拒絕 loopback/private/
  link-local/multicast/metadata、**解析 DNS 後再檢查所有位址**（只看第一個是繞過
  方式）；自架內網上游需明確 `POPO_ALLOW_PRIVATE_UPSTREAM=1`。
  → **授權移到 probe 之前**。順序本身就是安全屬性，測試用「probe 一旦被呼叫就
  失敗」的間諜物件釘住（`test_setup_ssrf`，14 項）。
- **全域併發無上限**：thread-per-request 在突發流量下會建立無界執行緒與上游請求。
  → `popo/server/limits.py`：有界計數信號、等待有期限（超時回 429 而不是無限排隊）、
  slot 在 `finally` 釋放（洩漏等於慢性停機）。推論／直通／adult 路由全部經過它。

### 證據誠信（稽核最重要的一點）

- **原本的「parity」腳本從不執行 V1**：它只啟動 V2 連 mock。把它列為 parity 證據
  會讓完成度看起來高於實際。
  → 改名為 `V2 environment checks`（只宣稱 V2 契約），並新增真正的差分工具
  `tests/parity/v1_diff.py`：實際 import V1、跑同一組語料、逐項比較。
  → 差分結果：**破甲 prompt 逐字元相同**（glm/deepseek/gpt 四個家族）、
  拒答分類 6/6 一致、歷史清理 2/3 一致，另 2 項是**已調查的刻意差異**
  （V1 會把使用者文字截掉一個字元並丟棄較早的 user turn）。
  → V1 不存在時工具以 exit 2 結束並印「這不是通過」；把「無法比較」當成
  「比較通過」是最容易製造假信心的捷徑。

### 效能（稽核 P2）

- **連線池形同虛設**：`SourceFactory` 每次請求都建新的 `UpstreamSource`，池子在
  被回收前不可能重用。
  → 每個 process 一個 factory，keyed on config 物件（設定換了會重建並關閉舊池）。
  → 證據改成**伺服器端計數**：`test_pool_reuse` 斷言 3 次請求只開 1 條 TCP。

### 跨平台與輸出（稽核 P1/P2）

- **Windows 上 2 個權限測試必然失敗**：它們斷言 POSIX `0600`。→ 改為
  `skipUnless(os.name == "posix")`，並在測試裡寫明這是平台限制而不是假裝安全。
- **CP950 終端機讓 parity 死在 `↔`**：→ 啟動時把 stdout/stderr 設為 UTF-8。
- **`test_real_upstream` 在預設 suite 執行**，卻需要不存在的 `hark-model-use`。
  → 改為 `POPO_ONLINE=1` 才跑。

### 診斷相容性

- 補回 V1 的 `X-Armor-Request-Model`、`X-Armor-Upstream-Model`，新增
  `X-Armor-Attempts`、`X-Armor-Score`、`X-Armor-Classification`。
- **`retry_max` 語意寫清楚**：它算的是**總呼叫次數（含第一次）**。預設 8 代表最多
  8 次呼叫、7 次重試，不是 8 次重試。`RetryPlan.max_retries` 提供另一種讀法。

### 測試

249 單元 + 39 整合 + 22 能力契約 + 11 V2 環境檢查 + 14 V1/V2 差分，全綠。

### 仍然未完成

- **真實上游的破甲效果與成本仍未量測**（工具就緒，需 operator 金鑰）。
- V1 的多來源/模型路由、`/v1/responses` 等完整端點面、context handoff、media 功能
  尚未移植；`FEATURE_MATRIX.md` 已逐項標示，不再宣稱全部完成。
- 稽核是 heuristic；沒有負載測試。

## [2.3.0] — 2026-11-09

第三輪：把 2.2.0 誠實列出的兩個 PARTIAL 收掉，並補上「唯一能驗證破甲有效」的工具。

### 修正（2.2.0 遺留）

- **串流取消只停寫入，上游仍在生成**：客戶端斷線後 provider 繼續產生並計費
  token，只是沒人讀。→ `UpstreamSource.stream()` 改回傳可中止的 `UpstreamStream`；
  客戶端斷線時 `abort()` 會 `shutdown()` 上游 socket，讓 provider 停止產生。
  **這是花錢的 bug**，所以測試直接觀察上游端：`test_stream_cancel` 斷言串流
  沒有跑到 200 個 chunk 就停了。
- **重寫時自己引入的回歸**：把上游串流改成 `UpstreamStream` 後，緩衝階段的
  `for … break` 會觸發生成器 `finally` 而提前關掉 socket，導致正常串流只剩
  2 個 chunk。→ 改成顯式 `read()`，並在邊界接受兩種形狀（handle 或 iterator），
  測試 `test_stream_cancel.test_full_stream_completes` 守住。

### 新增

- **調校端點** `/admin/tuning`（GET 讀取含範圍，POST 套用）：9 個可熱改欄位，
  每個都有上下限；**超出範圍直接拒絕，不自動夾住**（夾住會把打錯字藏起來）。
  `bind_port` 這類需要重啟的欄位會回「需要重啟」而不是假裝已套用。
  整批驗證通過才套用——部分失敗不會留下半套設定。
- **調校持久化** `tuning.json`（0600）：重啟後不會靜默回退；壞檔只印警告，
  不合法的舊值單獨拒絕，不影響其他設定。
- **控制台調校分頁**：欄位與範圍由伺服器提供，前端不自己發明。
- **受控真實上游 smoke** `tools/smoke_real.py`：`--cases` 與 `--max-spend`
  都是硬上限（送出前先計數），`--armor-off` 可跑基線對照，輸出 verdict /
  重試次數 / 字數 / 是否含程式碼 / 具體值數量。它**不宣稱答案正確**，只回報
  有沒有交付、內容長什麼樣。

### 測試

217 單元 + 34 整合 + 22 能力契約 + 11 V1 行為對照，全綠。

新增守門：串流斷線必須讓上游停止（觀察伺服器端，不是只看我們這邊）、
調校欄位不得無界、調校不得部分套用、smoke 工具不得內含憑證。

### 仍然未完成

- 真實上游的破甲效果與成本**仍未量測**：工具就緒，但需要 operator 的上游金鑰。
  在那之前「破甲有效」是設計主張，不是實測結論。
- 稽核是 heuristic，不是正確性證明。
- 沒有負載測試（併發只驗證到 8 路）。

## [2.2.0] — 2026-11-09

第二輪：把 V1 剩餘的可交付能力補完，並修掉第一輪只顧架構、沒顧語意的地方。

### 修正（第一輪重寫引入的實質缺陷）

- **評分器把短而正確的回答判為未交付**：一句「hello」對「hi」被當成沒有成品，
  於是每個請求都燒掉 8 次重試才交出回應——成本 8 倍，而且重試可能更糟。
  → 交付判定改為閘門（規避/缺少被要求的成品才算失敗），分數只用來排序。
  新增 `test_delivery_decision.TestCase.test_short_correct_answer_is_not_retried`
  把它釘死。
- **`list_conversations` 把每個對話 ID 截掉一個字元**：`fn[:-7]` 對 `.jsonl`
  多切一位，導致 `/admin/conversations/<id>` 一律 404、稽核讀不到對話。
  → 改用 `-len(".jsonl")`，並由 `test_audit` 的五個案例守住。
- **上游錯誤訊息被當成答案交付**：provider 回 `Model 'x' not found...` 但 HTTP 200，
  分類器看不到（`choices` 存在、內容非空）。→ 新增 `upstream_error` 分類，
  不交付、也不浪費重試（同一條壞路會回一樣的話）。

### 新增

- **判定稽核**（`popo/storage/audit.py`）：讀實際記錄，找出「決策與結果互相矛盾」
  的回合（`retry-burned` / `delivered-refusal` / `ok-but-empty` / `thin-delivered`）。
  明確標示為 heuristic，不宣稱證明安全。
- **請求重放**（`popo/server/replay.py`）：記錄不足以重建就**拒絕**，不猜一個 body。
  `stable` 在只有一次取樣時回 `null`，而不是假裝穩定。
- **模型比較**：模型數與重複次數都有硬上限，並回報實際呼叫次數（成本可見）。
- **保留期限清理**：無法解析的行**保留**，不因為看不懂就刪掉歷史。
- **首次設定 bootstrap token**：一次性、有期限、只存雜湊、0600；用了就失效。
  已設定的安裝不需要它——否則會把正常運作中的實例鎖在門外。
- **systemd 產生器**：憑證一律走 `EnvironmentFile`，單元檔內不內嵌；
  附 `ProtectSystem` / `NoNewPrivileges` / `ReadWritePaths` 等收斂。
- **隧道 URL 發現**：只接受已知後綴（`*.srv.us`、`*.trycloudflare.com` 等），
  類似網域（`srv.us.evil.example`）不算；找不到就回「沒有」，不編一個。
- **控制台雙語接線**：`data-i18n` 標記 + 語言切換；`i18n.js` 可在瀏覽器外執行，
  由 `test_console_i18n` 用真的檔案跑 key 比對。
- **PWA**：`<link rel="manifest">`、theme-color、`scope`、`lang`。
- **跨平台 CI**：Ubuntu/Windows/macOS × Python 3.10/3.12；另有一個 job 連打包
  可重現性與「解壓後再測」一起驗。
- `python3 -m popo --systemd` / `--tunnel` / `--check` / `--status`。

### 新增測試

200 單元 + 30 整合 + 22 能力契約 + 11 V1 行為對照。

新增的守門：每條宣告路由都必須有實作、路由名稱不得重複、admin 路由必須
operator-only、CI 引用的每個腳本必須存在、打包兩次要同 sha256、
乾淨解包後要能跑完整套件。

### 仍然未完成

- 串流期間客戶端斷線未主動取消上游請求（只停止寫入）。
- 控制台調校頁 UI 未建。
- 真實上游的破甲效果與成本未驗證；所有證據來自 mock upstream。

## [2.1.0] — 2026-11-09

以 V1 2.7.0 為功能基準的架構重寫。**沒有搬運 V1 模組**：行為契約重新實作，模組邊界重新設計。

### 架構

- **HTTP handler 只剩分派**：`popo/server/app.py` 由 742 行降至 245 行，只做事
  請求解析 → 路由 → 授權 → 呼叫 service → 寫回應。
- **路由表集中宣告權限**（`server/routes.py`）：`public` / `client` / `operator`
  寫在路由上，dispatcher 在 handler 執行前強制檢查，新路由不可能忘記加驗證。
- **端點實作按領域分檔**（`server/endpoints.py`）：發現、設定、推論、session、
  記錄、管理各一組 mixin，互不相干。
- **依賴組裝獨立**（`server/deps.py`）：store、gate、CORS 由一處建立並可注入。
- **回應只做傳輸**（`server/responses.py`）：狀態碼、標頭、SSE 框架，不含領域判斷。
- **管理端純函式化**（`server/admin.py`）：輸入 dict、輸出 dict，可無伺服器測試。
- **設定改為別名表 + 值物件**（`config.py`）：`ENV_ALIASES` 一張表列出所有設定與
  1.x 變數對照，只在建構時讀環境；`ARMOR_OFF` 這類反向命名集中在
  `INVERTED_ALIASES`，不再散落 `not`。

### 修正（V1 遺留的實質缺陷）

- **串流會丟棄內容**：舊管線在判定「clean」後只回傳已緩衝的 chunk，之後的
  upstream 資料全部丟掉。現在只有開頭會被緩衝，判定後立刻續傳，並明確標示
  commit point——一旦寫給客戶端就不能再假裝重試成功。
- **passthrough 繞過模型發布政策**：`/v1/images`、`/v1/embeddings`、
  `/v1/audio` 原本不檢查發布清單。
- **CORS 反射任意 Origin**：原本直接回傳呼叫方的 `Origin` 並帶
  `Allow-Credentials: true`，等於任何網頁都能讀。現在只認伺服器端事實
  （bind 位址、設定的 public base、明確白名單），`Origin: null` 預設拒絕。
- **同機瀏覽器可繼承 loopback 信任**：現在帶 `Origin` / `Sec-Fetch-*` 的
  loopback 請求不再被當成本機工具。
- **金鑰檔損壞即靜默清空**：改為隔離成 `.corrupt-<時間>` 並印出告警。
- **帳戶檔損壞即靜默清空**：同上。
- **密碼只做 SHA-256(salt+password)**：改用 PBKDF2-HMAC-SHA256（可設定迭代數），
  舊雜湊在成功登入時透明升級；比較一律常數時間。
- **session 只存記憶體、重啟即失效**：改為原子落盤 + TTL，撤銷即時生效。
- **`sk-good` 這類短假金鑰逃過掃描**：發佈檢查改為「非範例設定檔出現任何非
  placeholder 的密鑰欄位就失敗」，並實際掃描最終 tarball。
- **`_read_body` 讀完才檢查大小**：改為讀取前拒絕。
- **未知名路徑被轉發上游**：現在回 `proxy.no_route`。
- **admin 金鑰只存在記憶體雜湊集合**：現在同時註冊到 keystore，可用同一憑證
  管理與呼叫。

### 新增

- `/v1/images/generations`、`/v1/embeddings`、`/v1/audio/transcriptions`
  （multipart 原樣轉發，不解碼不重編碼）。
- Anthropic 非串流完整形狀（`usage`、`stop_reason` 對照）與 SSE
  `content_block_delta` 轉譯（`[DONE]` 原樣通過）。
- `x-api-key` 認證（Anthropic 客戶端慣例）。
- `X-Armor-Level` 逐請求覆寫，白名單外的值直接 400（不是靜默忽略）。
- 串流回報真實 verdict：重試成功為 `retry_ok`，預算耗盡仍拒答為 `retry_failed`。
- 登入失敗限流（有界記憶體、滑動視窗、loopback 豁免避免自我鎖死）。
- `/admin/usage`、`/admin/accounts`、`/routes`。
- `python3 -m popo --check` / `--status` / `--version`。
- 工具：`tools/architecture_check.py`、`tools/check_release_tree.py`、
  `tools/build_release.py`（可重現打包）。

### 測試

- 101 單元 + 18 整合 + 22 能力契約 + 11 V1 行為對照，全部通過。
- 新增負向測試：偽造 Origin、同機瀏覽器、未發布模型、超大 body、未知路由、
  超過 300 行的函式、超過 700 行的模組、循環匯入、模組層環境讀取、
  500 個不同身份的限流不爆表。
- **乾淨解包後重跑全套**：通過。

### 已知限制

- sentinels 剝除尚未接進請求路徑。
- trace 稽核、請求重放、模型比較、保留期限清理尚未移植。
- 隧道自動發現、systemd 產生、CI workflow 尚未建立。
- 串流斷線只停止寫入，未主動取消上游請求。
- 只測過 mock upstream；真實提供者的破甲效果與成本需要接真上游驗證。

## [2.0.1] — 2026-11-09

（前次發佈，見先前的封存紀錄。）

---

## Archive — Prism / Triad 1.x

The previous single-file proxy. Entries below are reproduced verbatim (headings demoted one level) and describe code that is no longer in this repository. They are kept so the history is not rewritten.

### [2.6.0] — 2026-11-07

#### Decoupled & Refactored

- **Extracted `armor_refusal.py`**: Decoupled anti-refusal heuristic classification, unicode folding
  (`_norm_unicode`), opener normalization (`_opening`), passive phrase masking (`_mask_passive`),
  plain-text pseudo-tool-call detection (`looks_like_pseudo_call`), markdown script detection
  (`looks_like_script_instead_of_call`), technical content salvaging (`salvage_technical`), essential
  command extraction (`essential_commands`), repeat turn detection (`is_repeat_turn`), trivial probe
  filtering (`is_trivial_call`), answer acceptability testing (`_answer_acceptable`), and candidate
  retry attempt ranking (`_attempt_score`).
- **Extracted `armor_scrub.py`**: Decoupled conversation history scrub engine (`scrub_refusals`),
  refusal turn detection (`_is_refusal_turn`), empty nudge turn detection (`_is_empty_nudge`),
  prose-only turn detection (`_is_prose_only_turn`), planning narration turn detection (`_is_narration_turn`),
  and orphan user turn collapsing (`_collapse_orphan_users`). Supports dynamic proxy state lookup
  to maintain full compatibility with runtime configuration toggles.
- **Architectural Cleanup in `armor_proxy.py`**: Cleaned up duplicated heuristic constants and orphan
  code blocks, re-exporting all refusal and scrub engine primitives with 100% backward compatibility.
  Reinforced SSE stream sniffing (`sniff_stream`) and in-band refusal recovery pipelines.

#### Optimized & Internationalized

- **Web Console Keys View Localization**: Added dynamic multi-language localization (`zh-Hant`, `zh-Hans`, `en`)
  for connection mode badges, upstream statuses, and helper notes in `console/keys.js` and `console/i18n.js`,
  with automatic reactive re-rendering on `forge:langchange`.
- **Runtime Session State Isolation**: Configured `.gitignore` to prevent runtime `proxy/session_state.json`
  from leaking into version control.
- **Expanded Offline Test Matrix to 33 Suites**: Added dedicated unit test suites `test_armor_refusal.py`
  and `test_armor_scrub.py`. All 33 suites in `run_tests.py` (including comprehensive virtual E2E in
  `test_theoretical_features_e2e.py`) pass 100% with zero third-party dependencies.

---

### [2.5.0] — 2026-11-07

#### Decoupled & Refactored

- **Extracted `armor_pool.py`**: Modularized upstream HTTP/HTTPS connection pooling, socket liveness
  probing, write-retry handlers, and TLS context caching out of `armor_proxy.py`. Re-exported all
  symbols with 100% backward compatibility for existing scripts and callers.
- **Extracted `armor_headers.py`**: Decoupled WAF bot signature sanitization (`_normalise_upstream_headers`),
  edge-block heuristic detection (`_looks_like_edge_block`), reverse-proxy client IP resolution
  (`_forwarded_client`, `_ip_from_headers`), loopback detection (`_is_loopback`), and monotonic
  request ID generation (`_rid_now`).
- **Extracted `armor_limiter.py`**: Separated sliding-window rate limiting into a dedicated module with
  automated memory eviction (`prune()`) to prevent unbounded memory growth from ephemeral identities.
- **Decoupled Anthropic Shape Detection**: Moved `looks_anthropic_body` into `anthropic_shape.py` and
  re-exported it in `armor_proxy.py`.

#### Optimized

- **Streaming Premature Disconnect Handling**: In `armor_proxy.py` streaming loop, premature client
  disconnection (`BrokenPipeError`, `ConnectionResetError`) now sets `client_gone = True`, preventing
  wasteful secondary writes and exceptions when flushing synthetic `[DONE]` or comment frames.
- **Web Console Account Modal Localization**: Added full tri-lingual i18n support (`zh-Hant`, `zh-Hans`,
  `en`) for all strings, placeholders, and error messages in `console/account.js` and `console/i18n.js`.
- **Expanded Offline Test Matrix to 31 Suites**: Created dedicated unit test suites `test_armor_pool.py`,
  `test_armor_headers.py`, and `test_armor_limiter.py`. All 31 suites in `run_tests.py` and 45 assertions
  in `test_theoretical_features_e2e.py` pass cleanly.

---

### [2.4.0] — 2026-11-07

#### Fixed

- **Fresh install bootstrap login deadlock**: In `acct_http.py`, `/a/login` now automatically
  calls `ensure_owner` when `count_users() == 0`, allowing the operator to create the initial
  owner account directly from the Web Console without being blocked by "帳號或密碼錯誤".
- **Console login overlay modal trapping**: Added dismissible controls (`✕` and "稍後設定")
  to `acctLoginScreen` in `console/account.js`, preventing the full-screen modal from holding
  the operator hostage or blocking access to Setup and Chat views.
- **Same-origin account API routing**: Fixed `acctRoot()` in `console/account.js` to prioritize
  same-origin relative paths over `cfg.base`, preventing account requests from being misrouted
  to external upstream model providers.

#### Added

- **Co-creator copyright declarations**: Added explicit joint copyright ownership declarations
  attributing **asas7412589630-hash** and **Minglink** across `README.md`, `README.en.md`, and
  the Web Console About view.
- **Native Windows CLI account management**: Added `acct.bat` and `acct.ps1` wrappers for Windows
  CMD and PowerShell, and integrated `python triad.py acct <cmd>` subcommand delegation.
- **End-to-end bootstrap login test**: Added Web bootstrap login and session persistence assertions
  to `test_theoretical_features_e2e.py` (now 45/45 passing).

---

### [2.3.0] — 2026-11-07

#### Added

- **Full Anthropic Messages API Translation**: Unlocked bidirectional `/v1/messages` <-> OpenAI
  `/v1/chat/completions` translation in `armor_proxy.py`, enabling native Claude Code and
  Anthropic SDK compatibility with automated persona injection and refusal recovery.
- **28-Suite Unified Offline Test Matrix**: Integrated `test_clients` and `test_concurrency` into
  the offline suite runner with hermetic `QuietServer` harnesses, expanding the test net to 28
  passing test suites and 39 virtual theoretical verifications.

#### Fixed

- **Debug log unbounded growth**: Replaced direct unbounded writes to `FP_DEBUG_FILE` and
  `RESP_LOG` with size-bounded, rotating log management (`_append_jsonl`) adhering to
  `ARMOR_EPHEMERAL` zero-leak requirements.
- **Anthropic upstream routing defect**: Fixed `_forward_locked` upstream route path resolution
  which previously sent `/v1/messages` to OpenAI upstreams instead of the translated `/v1/chat/completions`.
- **Windows socket noise in test servers**: Suppressed spurious `ConnectionResetError` (WinError 10054)
  traces during test harness client disconnects.

---

### [2.2.0] — 2026-11-07

#### Added

- **Web Console i18n Engine (`console/i18n.js`)**: Real-time multi-language support
  (Traditional Chinese `zh-Hant`, Simplified Chinese `zh-Hans`, English `en`) with
  automatic browser language detection, `localStorage` persistence, and instant DOM
  binding via `data-i18n` attributes.
- **Cross-platform Automated Test Runner (`run_tests.py`)**: Unified Python runner that executes
  all 26 test suites in isolated subprocesses with dynamic port allocation and comprehensive
  summary metrics.
- **Windows CI Pipeline (`offline-suites-windows`)**: Integrated Windows runner matrix on GitHub
  Actions across Python 3.11 and 3.12, continuously verifying cross-platform compatibility.

---

### [2.1.0] — 2026-11-07

#### Added

- **Cross-platform CLI manager (`triad.py`)**: Native support for starting, restarting,
  stopping, and health checking the proxy across Windows, macOS, and Linux without bash.
- **PowerShell launcher (`triad-up.ps1`)**: Native launcher for Windows environments.
- **`AUTHORS.md`**: Formal co-creator and maintainer documentation (Minglink and asas7412589630-hash).
- **Virtual Real-World E2E Suite (`test_theoretical_features_e2e.py`)**: Comprehensive
  verification harness asserting 29 critical theoretical claims: multi-model prompt
  invariants, dynamic refusal recovery, streaming SSE sidecars, context hygiene,
  multi-tenant accounts, token minting/revocation, and storage bounds.

#### Fixed

- **Trace storage unbounded growth**: Implemented `prune_traces(keep_days)` in `trace_rec.py`
  with automatic periodic retention pruning (`ARMOR_TRACE_KEEP_DAYS`), closing the known
  limitation of unbounded daily `.jsonl` accumulation.
- **Global UTF-8 encoding**: Explicitly enforce `encoding="utf-8"` on all text `open()`
  calls across test suites and core tools, fixing `UnicodeDecodeError` on GBK/CP936 Windows.
- **Windows permission compatibility**: Fixed POSIX `0o600` permission assertion in
  `test_model_sources.py` on Windows NTFS.
- **Graceful shutdown on Windows**: Added `SIGBREAK` signal handler to `armor_proxy.py` and
  `CTRL_BREAK_EVENT` handling in `test_shutdown.py`, resolving abrupt process termination.
- **Test isolation**: Prevented test suites from writing transient credentials to the
  repository tree during execution.
- **Test matrix**: 25/25 offline test suites now pass cleanly on both Windows and Linux.

---

### [2.0.0] — 2026-11-06

First public release. The number is 2 rather than 1 because an earlier internal
build existed; nothing from that lineage is supported.

#### Added

- **Ephemeral mode** (`ARMOR_EPHEMERAL=1`). Sends nothing to disk: no request
  log, no turn trace, no archive, no usage ledger, no accounts file, no setup
  token file. Verified by sending requests with a unique marker and grepping the
  tree — zero hits, zero files.
- **`triad-deploy`** — hosted setup that defaults to ephemeral + loopback, emits
  a systemd unit, and refuses a public bind when no upstream key is set.
- **`proxy.no_route`** error kind. An unknown path is now reported as "this proxy
  does not serve that route" instead of being forwarded and coming back as
  `upstream.model_not_found`, which sent operators to debug the model table.
- **`upstream.edge_blocked`** error kind. A CDN/WAF rejection (HTML body,
  `cf-mitigated`, `cf-error`) is no longer reported as `upstream.auth`.
- **Outbound header normalisation.** Library User-Agents (`python-urllib`, `curl`,
  `wget`, `java`, `go-http-client`) are rewritten and `Accept-Language` / `Accept`
  filled in, because a WAF tends to block those before the request reaches a model.
  `ARMOR_UPSTREAM_UA` overrides.
- **`/setup/token`** — same-origin bootstrap credential, 32 random bytes, refused
  once the first client key exists.
- **HTTP method coverage.** `PATCH`/`TRACE`/`CONNECT` and every error the base
  class raises now answer in the proxy's JSON envelope instead of an HTML page
  that advertises the Python version.

#### Security

- **Admin writes require a real credential.** Replaces `X-Armor-Admin: 1`, a
  constant in the console's own JavaScript that authenticated nothing. One
  remote request could mint a permanent key, dump the transcript and overwrite
  the upstream credential.
- **CORS trust is derived from server-side facts.** The old check compared the
  request's `Origin` against its own `Host` header — both caller-controlled — so
  `Host: evil.example` + `Origin: http://evil.example` was answered as trusted.
- **Loopback no longer skips auth for browser-driven requests.** A page on the
  same device reaches `127.0.0.1` like curl does; `Origin` / `Sec-Fetch-*` reveal
  which is which. Closes the CSRF side-effect path CORS cannot cover.
- **A fresh install is no longer an open relay.** Previously every endpoint
  answered while the key store was empty, with one log line as the only warning.
- **`Handler.timeout = 60`.** 200 stalled connections used to pin 201 threads for
  at least four minutes; they now close within the minute.
- **`Origin: null` refused by default** (`ARMOR_CORS_ALLOW_NULL=1` to opt in).
- **401/403 key rotation is bounded to one pass** over the pool. It used to spend
  the entire 90 s budget on a single dead key.

#### Fixed

- **`REQUIRE_CLIENT_KEY` was a local variable**, so switching 對外/對內 printed
  success, `healthz` agreed, and nothing changed until a restart.
- **`triad-up --restart` could leave the install dead.** A fixed 0.6 s sleep is
  shorter than the proxy's own drain, so the new process hit `Address in use`,
  the old one exited, and the pidfile pointed at a corpse.
- **Context scrubbing deleted the operator's own request.** `_is_empty_nudge()`
  treated any user message of ≤12 characters as a contentless prod, so
  `破解软件` (four characters) was removed along with the refusals it followed,
  and the model answered a leftover identity question. The classification is now
  a whitelist of actual continuation words, the last user message is protected
  unconditionally, and a scrub that would leave no user message is rolled back.
- **Narration removal defaults off.** Keeping a plan turn in history measured as
  good or better than deleting it (1062 chars/23 terms vs 978/10), because a plan
  turn is context. `ARMOR_SCRUB_PROSE=1` still enables the old behaviour.
- **`bench.py` resolved `../proxy/`** — a path from before the package moved into
  its own directory — and indexed `[0]` on a possibly empty key list. Every tool
  importing it (`rewriter`, `effort_tune`, `armory`, and through `structural`,
  the reframe layer that runs on every request) died at import, so reframe had
  been silently dead since packaging.
- **Unpublished namespaced aliases were routable.** `srcdemo/beta-9` for a model
  that was never published fell through to the shared provider and answered.
- **`test_sources_integration.py` depended on the live internet.** Without
  `ARMOR_UPSTREAM_HOST` an unpublished alias reached the real provider, which
  takes ~25 s to 404 an unknown model — longer than the client timeout, so the
  suite failed in a way that looked like a proxy bug. Now hermetic.

#### Project

- **Licence: CC BY-NC 4.0.** Commercial use is prohibited; contact
  asas7412589630@gmail.com for a commercial licence. Chosen over MIT because the
  armour text is the part worth protecting, and an MIT grant would let anyone
  resell it.
- **Contact** for bug reports, feature requests and security issues:
  asas7412589630@gmail.com (or a GitHub issue / private advisory).
- **Roadmap** published in the README: more model families, console i18n,
  and a longer-term move to a website and desktop application.

#### Known limitations

- `_forward_locked` is 2,149 lines in one function. Splitting it needs a golden
  test recording real upstream traffic first; a refactor without one has already
  cost a debugging cycle.
- Log rotation covers the request log and daily trace turns (`prune_traces`). Debug files still grow
  unbounded in non-ephemeral mode without a prune cron.
- WAF-blocked egress IPs are not fixable from inside the proxy.

---

Made by your-domain.example :)
