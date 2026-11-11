# POPO 2.7.13

POPO 是一個以 Python 標準庫實作的模型 API 閘道。它提供簡化的本機控制台、多上游模型路由、可選的模型提示積木、請求調校與使用紀錄。

## 快速開始

需求：Python 3.10 以上；核心服務不需要安裝第三方套件。

在專案目錄啟動服務：

```powershell
# Windows
py -3.10 -m popo.server.app
```

```sh
# macOS / Linux
python3 -m popo.server.app
```

開啟 [http://127.0.0.1:8412/app/](http://127.0.0.1:8412/app/)，控制台以本機同源方式管理，不需要登入。首次設定依序填入上游 Base URL 和 API Key，讀取模型、勾選要發布的模型及提示積木，選擇新 API 金鑰範圍，再按「套用設定並建立 API」。新建立的用戶端 API 金鑰只會顯示一次，請立即複製。

積木選項包含 DeepSeek、GLM、Kimi、國模通用、不注入和 GPT-5.2。DeepSeek、GLM、Kimi、國模通用共用同一組 V1 Cinder + 交付契約積木；GPT-5.2 與不注入維持獨立選項。設定會保存並套用到下一個推論請求。若上游不提供 `/v1` 模型列舉，請在「進階連線設定」確認路徑前綴。

## 功能

- OpenAI Chat Completions 相容代理，以及 Anthropic Messages 轉譯
- Images、Embeddings、Audio Transcriptions 直通端點
- 可選的成人媒體相容端點（預設停用）
- 多上游來源、模型發現、連線探測、模型發布與每模型提示積木
- 拒答處理與有界重試、歷史整理、上下文壓縮、長任務交接與摘要後輪替
- 用戶端 API 金鑰管理、請求與用量紀錄、稽核、重放、比較及保留期限清理
- V1 四組調教：重試與評分、上下文整理、摘要與狀態保留、記錄與存取
- 分層回合紀錄：客戶端 → 對話 → 回合；展開可查看每次嘗試和完整回覆
- 可選 ephemeral 模式；無外部 CDN 依賴的繁體中文／英文控制台

功能狀態與已知限制見 [FEATURE_MATRIX.md](FEATURE_MATRIX.md)；模組與請求流程見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 個人使用與遠端部署

預設只綁定 `127.0.0.1`。本機同源控制台可以免登入管理。透過 LAN、公開網域或 tunnel 使用時，不會套用本機免登入；請先設定管理憑證，並在反向代理上啟用 HTTPS。可用 `POPO_LOCAL_ADMIN=0` 關閉本機免登入。

模型用戶端使用控制台建立的 API 金鑰。遠端管理可使用 `POPO_ADMIN_KEY` 或管理員帳號 session。不要把管理端點直接暴露在沒有存取控制的公開網路上。詳細資訊見 [SECURITY.md](SECURITY.md)。

### API 金鑰範圍

建立用戶端金鑰時可選「對內」或「對外」。對內金鑰只在本機或同一內網使用；對外金鑰可透過已驗證的公開網址跨網路呼叫。公開金鑰只授權模型推論端點，不能存取控制台管理 API。管理頁新建金鑰預設對內；首次設定保留對外預選，與舊版首次建立的跨裝置使用方式相容。舊版既有金鑰在升級後保留原有對外能力，可在下一次建立金鑰時選擇範圍。

## 常用環境變數

| 變數 | 說明 | 預設值 |
|---|---|---|
| `POPO_BIND` | 監聽位址 | `127.0.0.1` |
| `POPO_PORT` | 監聽埠 | `8412` |
| `POPO_DATA_DIR` | 設定、金鑰及紀錄目錄 | `data` |
| `POPO_EPHEMERAL` | 啟用零落地模式 | `0` |
| `POPO_LOCAL_ADMIN` | 允許本機同源控制台免登入 | `1` |
| `POPO_ARMOR` | 啟用模型提示積木 | `1` |
| `POPO_SCRUB` | 清理歷史中的拒答回合 | `1` |
| `POPO_RETRY_MAX` | 首次呼叫以外的額外重試次數 | `8` |
| `POPO_RETRY_ON_REFUSAL` | 拒答時自動重試 | `1` |
| `POPO_COMPRESS` | 整理較舊工具輸出 | `1` |
| `POPO_COMPRESS_AT` | 壓縮觸發字元數 | `55000` |
| `POPO_COMPRESS_TARGET` | 壓縮目標字元數 | `30000` |
| `POPO_FOLD` | 啟用中段歷史摺疊（有損） | `0` |
| `POPO_FOLD_AT` | 摺疊觸發訊息數 | `14` |
| `POPO_FOLD_KEEP` | 摺疊時保留最近輪數 | `8` |
| `POPO_CONTEXT_HANDOFF_AT` | 長任務交接 token 門檻；`0` 停用 | `0` |
| `POPO_CONTEXT_ROTATE_AT` | 有交接摘要後的歷史輪替門檻；`0` 停用 | `0` |

「調教」頁和 V1 一樣分四組。壓縮只修剪較舊工具結果；摺疊會用摘要替換中段歷史，原文仍留在管理員紀錄中。`capture_full` 預設關閉；開啟後完整請求與工具定義也會保存。回合全文與每次嘗試依 V1 的紀錄方式保存，所有瀏覽路由僅限管理員，ephemeral 模式不落盤。

第一次設定時，上游資訊會寫入資料目錄的 `popo.json`；來源金鑰另存於私有檔案。不要把 `data/` 或實際憑證提交到版本庫。

## 公開 API（選用）

控制台預設只綁定本機。要讓其他網路使用 API，開三個終端並都在專案根目錄執行：

1. 啟動 POPO：`python -m popo.server.app`（預設 API 埠 8412）。
2. 啟動公開 API 閘道：`python scripts/remote_api_gateway.py`（預設只監聽本機 18423）。
3. 啟動 SSH 隧道：`python scripts/remote_api_tunnel.py`。

第三個終端會顯示公開網址，隧道程式也會更新 `data/tunnel_url.txt`。控制台檢查 `/healthz` 後才會把公開位址視為可用。建立金鑰時選「對外」才能經此網址呼叫推論；管理、設定及對話紀錄路由不會經公開閘道轉送。保持三個程序運行；免費隧道網址可能輪替。Windows 可用 `POPO_PORT`、`POPO_GATEWAY_PORT` 和 `POPO_DATA_DIR` 設定埠及資料目錄，所有程序需使用一致設定。

## 開發與驗證

```sh
python tools/architecture_check.py
python tools/check_release_tree.py --allow-runtime
python -m unittest discover -s tests/unit
python -m unittest discover -s tests/integration
python tests/parity/v1_v2_parity.py
python -m unittest discover -s tests/parity -p "test_*.py"
```

以上測試使用 mock 上游，不會驗證每家供應商的實際行為。受控真實上游測試工具為 `tools/smoke_real.py`；執行前請設定明確的花費上限及測試憑證。貢獻方式見 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 授權與公開發布

此版本沿用 [CC BY-NC 4.0](LICENSE)，並未改授權。它限制商業用途，因此**不是 OSI 認可的開源授權**。發布者應先確認有權授權所有程式碼與內附內容，再決定是否改採 MIT、Apache-2.0 或其他授權；授權狀態與發布前工作列在 [OPEN_SOURCE_STATUS.md](OPEN_SOURCE_STATUS.md)。
