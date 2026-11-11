# POPO-V1 → POPO 2.x 全功能移植矩陣

> 完成標準：每列代表可獨立驗收的使用者能力。標為 DONE 必須有**可執行的測試路徑**。
> 更新：2026-11-10（2.7.7）
> 此矩陣只涵蓋已列出的功能契約；列項全數 DONE 不等於逐項證明整個 V1 完全等價。

## 狀態統計

| 狀態 | 數量 |
|---|---|
| DONE（有測試證據） | 54 |
| PARTIAL（有實作、缺關鍵子能力） | 0 |
| NOT APPLICABLE（附理由） | 3 |

## 代理核心

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| API-01 | OpenAI chat 非串流 | DONE | `test_proxy_mock_upstream`、`test_delivery_decision` |
| API-02 | OpenAI chat SSE（含 commit point） | DONE | `test_stream_manager`、`test_delivery_decision` |
| API-03 | Anthropic 非串流轉譯 | DONE | `test_proxy_surface.test_anthropic_non_stream_shape` |
| API-04 | Anthropic SSE 轉譯 | DONE | `openai_event_to_anthropic`（`[DONE]` 原樣通過） |
| API-05 | `x-api-key` 認證 | DONE | `test_anthropic_accepts_x_api_key` |
| API-06 | Models 發布過濾 | DONE | `test_setup.test_models_filtered_after_publish` |
| API-07 | Images 直通 | DONE | `test_images_passthrough_reaches_upstream` |
| API-08 | Embeddings 直通 | DONE | `test_embeddings_passthrough` |
| API-09 | Audio multipart 原樣轉發 | DONE | `test_audio_transcription_forwards_raw_multipart` |
| API-10 | 直通路由也驗發布政策 | DONE | `test_passthrough_rejects_unpublished_model` |
| API-11 | 未知路徑不轉發上游 | DONE | `test_unknown_route_is_not_forwarded` |

## 傳輸層

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| NET-01 | 連線池與跨請求重用 | DONE | `test_pool_reuse`（5，伺服器端計數：3 請求 1 連線） |
| NET-02 | 多金鑰輪替 | DONE | `test_upstream_keys`（4） |
| NET-08 | 多來源與 model→source 路由 | DONE | `test_sources` + `test_multi_source`（端到端含來源發現、probe、profile 與密鑰刪除） |
| NET-03 | header 正規化 | DONE | `headers_for` |
| NET-04 | 客戶端 metadata 不外洩 | DONE | `test_client_metadata_headers_not_forwarded` |
| NET-05 | WAF/edge 封鎖分類 | DONE | `_looks_like_edge_block` |
| NET-06 | 逾時與死線 | DONE | `upstream_timeout` / `retry_budget_seconds` |
| NET-07 | 串流取消上游 | DONE | `test_stream_cancel`（4，觀察上游確實停止產生） |

## 破甲

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| ARM-01 | 區塊化注入 + profile 解析 | DONE | `test_server.test_armor` |
| ARM-02 | 模型家族配對 | DONE | `blocks/registry.json` + `resolve_profile` |
| ARM-03 | `X-Armor-Level` 覆寫（白名單） | DONE | `test_armor_level_header_rejected_when_unknown`、`..._off_removes_system_prompt` |
| ARM-04 | 哨兵剝除（回音去重） | DONE | `test_salvage_scoring.SentinelTests`（5） |
| ARM-05 | `/armor` 預覽與 registry | DONE | `test_server`、`test_server_admin` |

## 拒答與重試

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| REF-01 | 拒答/空/上游錯誤/規避分類 | DONE | `test_capability_parity.ClassificationTests`（8） |
| REF-02 | 技術內容打撈 | DONE | `test_salvage_scoring.SalvageTests`（4） |
| REF-03 | 可交付性評分 | DONE | `test_salvage_scoring.ScoringTests`（9） |
| REF-04 | 有界重試 + 預算 | DONE | `RetryPlan`、`test_delivery_decision` |
| REF-05 | 按分類選壓力角度 | DONE | `pressure_for` |
| REF-06 | 最佳嘗試交付 | DONE | `_is_better` + `retry_best` |
| REF-07 | 串流 commit 後不假裝重試 | DONE | `test_stream_manager` |
| REF-08 | 上游錯誤不當答案交付 | DONE | 回 502，不再以 200 夾帶錯誤訊息 |
| REF-09 | token 預算耗盡不重試（reasoning 模型） | DONE | `test_truncation`（11）；實測 logfare glm-5.2 max_tokens=16 |
| REF-10 | 請求形狀判定（解釋 vs 產出） | DONE | `test_salvage_scoring` 形狀 5 項 |

## 上下文

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| CTX-01 | 拒答回合剝除 + 最後 user 保護 | DONE | `test_capability_parity.ScrubTests` |
| CTX-02 | fold / compress | DONE | `test_stateq`、`test_popo_context` |
| CTX-03 | 回寫前清除摘要 | DONE | `test_pipeline_context.test_long_conversation_compresses` |
| CTX-04 | 長任務交接摘要與摘要後輪替歷史 | DONE | `tests/unit/test_handoff.py`、`test_pipeline_context` |

## 身分與安全

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| AUTH-01 | 金鑰建立/停用/撤銷/標籤 | DONE | `test_keys`（5）、`test_server_admin` |
| AUTH-02 | 金鑰只存雜湊 + 常數時間比對 | DONE | `keys.verify` |
| AUTH-03 | 金鑰檔損壞隔離而非清空 | DONE | `_quarantine` |
| AUTH-04 | PBKDF2 密碼 + 透明升級 | DONE | `test_accounts` |
| AUTH-05 | session 持久化 + 撤銷 | DONE | `test_accounts.test_session` |
| AUTH-06 | 首次設定 bootstrap token | DONE | `test_bootstrap`（10）、`test_bootstrap_gate`（8） |
| AUTH-07 | 四層身分 | DONE | `test_gate`（7）、`RouteDeclarationTests` |
| AUTH-08 | 本機同源 loopback 可免登入管理；跨來源、轉送與公開 listener 不可 | DONE | `tests.unit.test_gate`（local admin、cross-origin、forwarded peer、public bind） |
| AUTH-09 | 伺服器事實 CORS + `null` 拒絕 | DONE | `test_cors`（7） |
| AUTH-10 | body 上限（讀取前拒絕） | DONE | `test_oversized_body_rejected_before_read` |
| AUTH-11 | 登入限流（有界記憶體） | DONE | `test_limiter`（6） |
| AUTH-12 | 每條路由宣告權限並強制 | DONE | `test_route_wiring`（9） |
| AUTH-13 | setup 不得被當成請求轉發器（SSRF） | DONE | `test_setup_ssrf`（14），含「授權先於 probe」順序證明 |
| AUTH-14 | 出口目的地政策（私網/metadata/scheme） | DONE | `popo/server/egress.py` + 同上 |
| AUTH-15 | 全域併發上限（有界等待、slot 必釋放） | DONE | `test_limits`（8，20 執行緒不超限、無洩漏） |

## 記錄與觀測

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| DATA-01 | 請求/trace/usage/對話記錄 | DONE | `test_ephemeral`、`test_pipeline_context` |
| DATA-02 | ephemeral 零落地 | DONE | `test_ephemeral`（3） |
| DATA-03 | 用量彙總 API | DONE | `admin.usage_summary` |
| DATA-04 | 判定稽核（heuristic） | DONE | `test_audit`（7） |
| DATA-05 | 請求重放（缺資料即拒絕） | DONE | `test_replay.ReplayTests`（6） |
| DATA-06 | 模型比較（成本有上限） | DONE | `test_replay.CompareTests`（6） |
| DATA-07 | 保留期限清理 | DONE | `test_retention`（6） |

## 控制台

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| UI-01 | 設定/金鑰/對話/紀錄/破甲頁 | DONE | `console/index.html` + `test_server.test_app_serves_index` |
| UI-02 | 雙語字典一致 | DONE | `test_console_i18n`（3，跑真檔） |
| UI-03 | 語言切換 + DOM 套用 | DONE | `test_console_assets`（5） |
| UI-04 | PWA manifest | DONE | `test_manifest_is_valid_and_installable` |
| UI-05 | 無 CDN 依賴 | DONE | `test_no_external_network_dependency` |
| UI-06 | V1 四組調教（重試/上下文/狀態/記錄存取） | DONE | `/admin/tuning` + `test_server_admin.test_v1_tuning_groups_apply_live_and_persist` |
| UI-09 | 客戶端→對話→回合紀錄與全文展開 | DONE | `/admin/trace/*` + `test_turn_traces` + `test_pipeline_turn_trace` |
| UI-10 | 每次重試與最終回覆的日分檔追蹤 | DONE | `TurnTraceStore` + `test_pipeline_turn_trace` |
| UI-07 | 單一設定流程保存並套用各模型提示積木 | DONE | `test_setup` profile 持久化 + `test_sources` profile picker + `test_console_assets` |
| UI-08 | 進階來源新增、列舉模型、probe、發布與刪除 | DONE | `tests.integration.test_multi_source` HTTP 端到端測試 |

## 部署與工程

| ID | 功能契約 | 狀態 | 驗收證據 |
|---|---|---|---|
| OPS-01 | start/stop/status/restart | DONE | `scripts/triad-up`、`python3 -m popo --status` |
| OPS-02 | 設定驗證 `--check` | DONE | `popo/__main__.py` |
| OPS-03 | 隧道 URL 發現（只認可信後綴） | DONE | `test_deploy.TunnelDiscoveryTests`（7） |
| OPS-04 | systemd 產生（無內嵌憑證） | DONE | `test_deploy.SystemdUnitTests`（7） |
| BUILD-01 | 白名單打包 | DONE | `tools/build_release.py` |
| BUILD-02 | 可重現（同內容同 sha256） | DONE | 實測兩次同值 + `test_workflow` |
| BUILD-03 | 最終 tarball 掃描 | DONE | `tools/check_release_tree.py` 對 staging |
| BUILD-04 | 乾淨解包後跑全套 | DONE | 實測通過 |
| BUILD-05 | 跨平台 CI | DONE | `.github/workflows/tests.yml` + `test_workflow`（7） |
| BUILD-06 | 受控真實上游 smoke | DONE | `tools/smoke_real.py` + `test_smoke_tool`（4）；有 `--max-spend` 硬上限 |

## 對照證據等級

| 證據類型 | 範圍 | 限制 |
|---|---|---|
| `tests/parity/v1_v2_parity.py` | V2 自身契約（11 項） | **不執行 V1**，只證明 V2 行為 |
| `tests/parity/v1_diff.py` | 真正執行 V1 與 V2 並比較（14 項） | 限於純函式：破甲組稿、拒答分類、歷史清理 |
| `tools/smoke_real.py` | 真實上游（logfare.ai） | n=6 快照，非統計顯著；見 `POPO-2.5.0-smoke/RESULTS.md` |
| `tests/unit`、`tests/integration` | V2 行為 | mock upstream；Provider 行為未驗證 |

差分結果：破甲 prompt **逐字元相同**（4 個模型家族）、拒答分類 6/6 一致、
歷史清理 3 項中 2 項一致，2 項為**已調查的刻意差異**（V1 會截斷使用者文字並丟棄
較早的 user turn；V2 保留）。

## 明確不移植（附理由）

| ID | 項目 | 理由 |
|---|---|---|
| NA-01 | V1 `armor_proxy.py` 單檔結構 | 正是本次要移除的技術債；行為已重寫 |
| NA-02 | V1 `cinder_server` / `armor_triad` 歷史分支 | 非產品線，與主線行為衝突 |
| NA-03 | 內建 persona 文字中性化 | persona 是產品內容；應由使用者以 `--write` 自行決定 |

## V1 結構問題 → 2.1 處置

| V1 問題 | 處置 | 守門 |
|---|---|---|
| 9,204 行單檔 / 2,200 行函式 | 最大模組 357 行、最大函式 < 300 行 | `architecture_check.py` |
| 158 個 import-time 環境變數 | 別名表 + 建構時讀取 | 同上（AST） |
| 185 個可變全域 | 依賴邊界在 handler 實例 | 同上 |
| 串流丟棄後續內容 | commit point 設計 | `test_stream_manager` |
| 直通繞過發布政策 | 直通也驗證 | `test_passthrough_rejects_unpublished_model` |
| CORS 反射任意 Origin | 只認伺服器事實 | `test_cors` |
| 金鑰/帳戶檔靜默清空 | 隔離 `.corrupt-*` 並告警 | `test_keys`、`test_accounts` |
| 密碼 SHA-256(salt+pw) | PBKDF2 + 常數時間 | `test_accounts` |
| body 讀完才檢查 | 讀取前拒絕 | `test_oversized_body_rejected_before_read` |

## 尚未完成（誠實列出）

- **真實上游實測已完成第一輪（glm-5.2 / logfare.ai）**：破甲與無破甲皆 6/6 交付、
  0 次重試、6 次呼叫。結論是「在這個模型上破甲無害但也無必要」——**不能**據此宣稱
  破甲有效。要驗證效果需要會拒答的模型（glm-5.3 / deepseek 系列）跑同一組 prompt。
- 稽核是 heuristic，不是正確性證明。
- 沒有負載測試（併發只驗證到 8 路）。
