# POPO 2.x 全功能重寫路線圖

## 目標

功能上完整涵蓋 V1 2.7.0 的可交付能力；架構上重新設計。每個切片需能單獨部署/測試/回退，禁止巨型搬移。

## 開發順序（依前置依賴）

### Slice 0 — 基線與守門（進行中）
- [x] checkpoint 保存改造前工作樹快照
- [x] 全功能矩陣 `FEATURE_MATRIX.md`
- [x] AST 架構閘門：單函式 300 行上限、module-level env read
- [x] release tree checker：runtime data/config/credentials 與二進位產物拒收
- [ ] CI 對上述檢查器執行
- [ ] 將現有超線函式切開；目前已知 `popo/server/app.py` 有 742 行，先拆 route dispatch / response writers / identity/admin handlers

### Slice 1 — 發佈可重現與設定安全
- 白名單打包腳本；只包含 source、docs、tests、blocks、console、scripts、必要工具
- release archive 解壓乾淨目錄後再做 check-secrets、env-read、compile、unit+integration
- 預設只附 `popo.example.json`，禁止 `popo.json`、data、logs、admin key、records
- checksum 可重現，忽略 mtime、建置順序與本機資料

### Slice 2 — API 外殼與傳輸
- 建立 router table/endpoint adapters，HTTP handler 保持薄
- chat/models、images、audio multipart、embeddings
- OpenAI ↔ Anthropic non-stream + SSE protocol adapter
- 上游 client pool、deadline/cancel、header allowlist、WAF error mapping
- parity fixtures：未知欄位、tools、multimodal、usage、finish reason、錯誤與 headers

### Slice 3 — Armor/拒答/重試
- registry/profile/levels/sentinel
- classifier、salvage、answer verdict 純函式模組
- bounded retry scheduler + 多 key/source picker
- streaming commitment：提交前可重試；提交後只可終止/標記，不偽裝透明重試
- corpus tests：拒答真陽性/真陰性、技術答案、短正確答案、upstream errors

### Slice 4 — Context/觀測與隱私
- fold/compress/state extraction parity
- storage ports + file/memory implementations + schema migrations
- trace/records/usage/account attribution/retention
- audit/replay/compare 有成本和重複次數上限
- ephemeral marker sweep、store fault injection、明文資料面清單

### Slice 5 — 身分/安全
- setup bootstrap token、帳戶 session、密碼 KDF
- route-level roles，loopback/browser/operator/client-key gate
- CORS/CSRF、body limits、rate limits、key revoke 即時失效
- 每條 route 的匿名/錯誤憑證/正確身份矩陣

### Slice 6 — UI/運維/外掛
- V1 所有控制台工作流程、多語言、PWA
- startup/shutdown/status/deploy/tunnel discovery/systemd
- add-on capability matrix：保留可交付能力與清楚 disable/failure reason
- platform CI (Alpine/Linux/Windows；其他支援平台按實際宣告)

### Slice 7 — 收尾與發佈
- `FEATURE_MATRIX.md` 每列均有狀態、證據、執行命令
- 所有 skip 都有原因；安全關鍵測試不得被默默 skip
- 功能回歸 + 架構閘門 + security scans + clean release test
- 新版本號、CHANGELOG、SHA-256、最終審計報告

## 當前第一個實作阻塞

現有 V2 `popo/server/app.py` 為 742 行，超出最初 300 行硬上限。架構掃描器目前會限制函式長度，但尚未限制模組長度或 handler 領域依賴；因此 Slice 0 還需先拆 app.py，並擴充 AST 架構檢查，不可把該掃描器誤稱為全面架構驗證。
