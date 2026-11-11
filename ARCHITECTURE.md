# POPO 2.7.8 架構文件

## 設計原則

1. `popo/` 核心服務使用 Python 標準庫，不需第三方 runtime 套件。
2. HTTP 路由、領域服務與儲存分層；路由權限在 `routes.py` 集中宣告。
3. 單一上游與多來源上游使用同一個 picker 介面，管線不需要知道供應商細節。
4. 請求解析、提示組稿、上下文管理、傳輸和回應轉換能各自測試。

## 推論請求生命週期

```text
HTTP Handler (`/v1/chat/completions`)
    │
    ├─ routes.py                    路由與身分權限宣告
    ├─ ChatRequest                   OpenAI / Anthropic 請求正規化
    ├─ SourceRouter / SourceFactory  選擇上游、模型與提示積木
    ├─ pipeline.py                   長任務交接、上下文壓縮、歷史整理與有界重試
    ├─ UpstreamSource                HTTP 傳輸及連線池
    └─ ResponseWriter                格式轉換、SSE 與錯誤回應
```

模型提示積木的設定優先序是：多來源路由上的 `profile`、一般安裝狀態中的 `model_profiles`、registry 對模型名稱的預設。使用者在控制台選擇家族後會保存至安裝狀態；若該家族有經測試的精確模型版本格式積木，解析器會同時套用該版本積木。明確選擇「不注入」或通用 profile 仍會作為覆寫。每次推論都會讀取目前設定，不需重啟服務。

## 模組邊界

| 模組 | 職責 |
|---|---|
| `popo.config` | 環境變數、安裝狀態與已遮蔽的設定檢視 |
| `popo.auth.gate` | API key、管理憑證、session 與本機同源管理身分 |
| `popo.auth.keys` / `accounts` | 用戶端金鑰與管理員帳戶資料 |
| `popo.server.routes` | HTTP 路由與權限宣告 |
| `popo.server.endpoints` | API handler、來源操作與上游探測 |
| `popo.server.setup` | 首次設定、模型列舉、發布與安裝狀態 |
| `popo.server.sources` | 不可變來源目錄、多來源 router、密鑰分開儲存 |
| `popo.server.tuning` | V1 四組調教欄位、驗證、即時套用與持久化 |
| `popo.core.request` | OpenAI / Anthropic 請求結構化 |
| `popo.core.pipeline` | 提示積木、壓縮、交接、重試與交付判斷 |
| `popo.core.upstream` | HTTP 連線池、上游請求、SSE 串流 |
| `popo.storage.records` | 請求、用量、長任務摘要、回合全文和狀態帳本 |
| `popo.storage.turn_traces` | 日分檔回合紀錄與客戶端／對話索引 |
| `popo.storage.ephemeral` | 零落地資料閘門 |
| `console/` | 不依賴外部 CDN 的繁中／英文控制台 |
| `blocks/` | 模型提示積木與模型家族 registry |

## 控制台身分

預設只綁定 loopback。本機同源瀏覽器只有在監聽位址、連線 peer、Host、Origin 和轉送標頭都符合本機條件時，才取得本機管理身分。跨來源、經 tunnel 轉送或公開 listener 不會取得免登入管理權。可用 `POPO_LOCAL_ADMIN=0` 關閉本機免登入。

遠端管理使用管理 API 金鑰或 owner/admin session。控制台不會把管理憑證或上游密鑰存到 `localStorage`。遠端部署需使用 HTTPS，並按 [SECURITY.md](SECURITY.md) 設定存取控制。

## 靜態檔案服務

- `/app/` → `console/index.html`
- `/app/app.js`、`/app/i18n.js` → 對應靜態檔案
- 路徑解析限制在 `console/` 內，無效路徑不會逃出目錄。

## 邊界與後續工作

- 多租戶隔離、分散式 session、負載測試和各供應商的真實端到端驗證不在本版本已驗證範圍內。
- Mock 上游測試只能證明 POPO 的 request path 與轉換行為，不能證明所有供應商完全相容。
- 以 [FEATURE_MATRIX.md](FEATURE_MATRIX.md) 記錄功能契約與測試證據；文件宣稱不能代替實測。
