# POPO 2.x 真實功能對照表

> 依 `popo/` 原始碼與可執行測試逐項標記。**沒有測試證據就不標完成。**
> 更新：2026-11-10（2.7.7；依原始 V1 行為與本版測試重新整理）

## 圖例

| 標記 | 意義 |
|---|---|
| ✅ | 已實作，有可重複執行的測試 |
| 🟡 | 部分實作或缺測試 |
| ❌ | 尚未移植 |
| ⛔ | 明確不移植（有理由） |

## 一、代理核心

| 功能 | 狀態 | 證據 |
|---|---|---|
| OpenAI `/v1/chat/completions` | ✅ | `test_proxy_mock_upstream`、`test_proxy_surface` |
| SSE 串流（含 commit point） | ✅ | `test_stream_manager`（4 項，含拒答重試與耗盡） |
| Anthropic `/v1/messages` 非串流 | ✅ | `test_proxy_surface.test_anthropic_non_stream_shape` |
| Anthropic SSE 轉譯 | ✅ | `popo/server/endpoints.py:openai_event_to_anthropic`（`[DONE]` 原樣通過） |
| `x-api-key` 認證 | ✅ | `test_proxy_surface.test_anthropic_accepts_x_api_key` |
| `/v1/models` 發布清單 | ✅ | `test_setup.test_models_filtered_after_publish` |
| `/v1/images/generations` | ✅ | `test_proxy_surface.test_images_passthrough_reaches_upstream` |
| `/v1/embeddings` | ✅ | `test_proxy_surface.test_embeddings_passthrough` |
| `/v1/audio/transcriptions`（原樣 bytes） | ✅ | `test_proxy_surface.test_audio_transcription_forwards_raw_multipart` |
| 未發布模型在 passthrough 也被攔 | ✅ | `test_proxy_surface.test_passthrough_rejects_unpublished_model` |
| header 正規化（UA / Accept-Language） | ✅ | `popo/core/upstream.py:headers_for` |
| 客戶端 metadata 不外洩 | ✅ | `test_proxy_surface.test_client_metadata_headers_not_forwarded` |
| WAF/edge 封鎖分類 | ✅ | `popo/core/upstream.py:_looks_like_edge_block` |
| 連線池 / keep-alive / 可重用性 | ✅ | `UpstreamSource._reusable`（未讀位元組即棄用） |
| 多金鑰輪替 | ✅ | `test_upstream_keys`（4 項） |
| 串流取消/斷線 | 🟡 | `BrokenPipeError` 已吞；上游未被動取消 |

## 二、破甲系統

| 功能 | 狀態 | 證據 |
|---|---|---|
| 區塊化注入 | ✅ | `armor/blocks.py` + `inject.py` |
| 模型家族配對 | ✅ | `blocks/registry.json` |
| `X-Armor-Level` 覆寫 | ✅ | `test_proxy_surface.test_armor_level_header_rejected_when_unknown`、`..._off_removes_system_prompt` |
| `/armor` 預覽 | ✅ | `test_server.test_armor` |
| `/armor/registry` | ✅ | `test_server_admin.test_armor_registry` |
| sentinels 剝除 | 🟡 | 函式在，request path 未接 |
| profile 解析 | ✅ | `bake_system_prompt` + `test_capability_parity` |

## 三、拒答與重試

| 功能 | 狀態 | 證據 |
|---|---|---|
| 拒答分類（拒答/空/上游錯誤/薄） | ✅ | `test_capability_parity.ClassificationTests`（8 項負向測試） |
| 長篇解釋不以拒答處理 | ✅ | `test_long_explanation_starting_with_opener_is_clean` |
| 程式碼區塊永不算拒答 | ✅ | `test_code_block_is_delivery_even_after_refusal_phrase` |
| provider 錯誤不當答案交付 | ✅ | `test_provider_error_text_is_not_delivered_as_answer` |
| 8 次上限 + 時間預算 | ✅ | `RetryPlan`（`can_retry` 同時檢查兩者） |
| 按分類選壓力角度 | ✅ | `pressure_for` / `_PRESSURE_FOR` |
| 壓力不污染原始 messages | ✅ | `test_base_messages_not_mutated` |
| `X-Armor-Verdict` / `Retries` / `Policy-Len` | ✅ | `test_capability_parity`、`v1_v2_parity` |

## 四、歷史清理

| 功能 | 狀態 | 證據 |
|---|---|---|
| 剝除拒答回合 | ✅ | `test_capability_parity.ScrubTests` |
| 最後一則 user 保護 | ✅ | 同上 |
| 清理後無 user 則回滾 | ✅ | `scrub_history` |
| 摺疊孤兒 user | ✅ | `collapse_orphan_users` |
| context fold / compress | ✅ | `test_popo_context`、`test_stateq` |
| 回寫前清除 stateq 摘要 | ✅ | `test_pipeline_context.test_long_conversation_compresses` |

## 五、身分與安全

| 功能 | 狀態 | 證據 |
|---|---|---|
| 路由宣告式權限 + 集中強制 | ✅ | `server/routes.py` + `test_capability_parity.RouteDeclarationTests` |
| loopback 工具信任 | ✅ | `test_loopback_tool_is_trusted_when_configured` |
| 同機瀏覽器不得繼承信任 | ✅ | `test_browser_on_loopback_is_not_trusted` |
| 匿名打 operator 路由被拒 | ✅ | `test_anonymous_cannot_reach_operator_route` |
| CORS 以伺服器事實為準 | ✅ | `test_cors`（7 項，含偽造 Origin / `null`） |
| 未知名路徑不轉發 | ✅ | `test_unknown_route_is_not_forwarded` |
| body 上限（讀取前拒絕） | ✅ | `test_oversized_body_rejected_before_read` |
| 金鑰只存 sha256 + 常數時間比較 | ✅ | `auth/keys.py:verify` |
| 金鑰撤銷即時生效 | ✅ | `test_keys.test_disable` |
| 無效金鑰檔隔離（非靜默清空） | ✅ | `auth/keys.py:_quarantine` |
| PBKDF2 密碼 + 透明升級 | ✅ | `auth/accounts.py` |
| session 持久化 + 撤銷 | ✅ | `test_accounts.test_session` |
| 登入限流（有界記憶體） | ✅ | `test_limiter`（6 項，含 500 身份不爆表） |
| 管理端金鑰/帳戶/用量 | ✅ | `server/admin.py` |

## 六、記錄與隱私

| 功能 | 狀態 | 證據 |
|---|---|---|
| 客戶端 → 對話 → 回合索引與按需展開全文 | ✅ | `storage/turn_traces.py` + `tests/unit/test_turn_traces.py` |
| 每輪嘗試、錯誤、最終回答與用量 trace | ✅ | `core/turn_recording.py` + `tests/unit/test_pipeline_turn_trace.py` |
| 請求/usage/對話記錄 | ✅ | `storage/records.py` |
| ephemeral 全鏈阻擋 | ✅ | `test_ephemeral` |
| 用量彙總 API | ✅ | `test_proxy_surface` + `admin.usage_summary` |
| trace 稽核（heuristic） | ✅ | 管理員稽核 API + `test_audit` |
| 請求重放 / 模型比較 | ✅ | `server/replay.py` + `test_replay` |
| 保留期限自動清理 | ✅ | `storage/retention.py` + `test_retention` |

## 七、控制台

| 功能 | 狀態 | 證據 |
|---|---|---|
| 設定 / 金鑰 / 對話 / 紀錄 / 破甲 | ✅ | `console/index.html` + `test_console_assets` |
| PWA manifest | ✅ | `console/manifest.webmanifest` + manifest 測試 |
| i18n zh/en | ✅ | `console/i18n.js` + `test_console_i18n` |
| V1 四組調教（重試/上下文/狀態/記錄存取） | ✅ | `/admin/tuning` + `test_server_admin` |
| V1 分層紀錄和回合詳情 | ✅ | `/admin/trace/*` + `test_turn_traces` |

## 八、部署與工程

| 功能 | 狀態 | 證據 |
|---|---|---|
| `python3 -m popo --check/--status` | ✅ | `popo/__main__.py` |
| 可重現打包（同內容同 sha256） | ✅ | `tools/build_release.py`（實測兩次同值） |
| 白名單打包（無 runtime/secret） | ✅ | `tools/check_release_tree.py` + 乾淨解包測試 |
| 架構閘門（300 行函式 / 700 行模組 / 無環 / 無 import-time env） | ✅ | `tools/architecture_check.py`（6 項自測） |
| 乾淨解包後全套測試 | ✅ | 2.7.7：362 unit + 48 integration + 22 parity + 11 V1 相容性檢查 |
| 隧道自動發現 / systemd 產生 | ✅ | `server/deploy.py` + `test_deploy` |
| CI workflow | ✅ | `.github/workflows/tests.yml` |

## 九、已消除的 V1 結構問題

| V1 問題 | 2.1 處置 |
|---|---|
| `armor_proxy.py` 9,204 行 / `_forward_locked` 2,200 行 | 最大模組 357 行；最大函式低於 300 行（有閘門擋） |
| 158 個 import-time 凍結環境變數 | `ENV_ALIASES` 表 + 只在建構時讀取（有閘門） |
| 185 個可變全域 | 依賴以 handler 實例為界，模組層無可變狀態（有閘門） |
| `except: pass` 靜默失敗 | 錯誤一律分類；只有「已送出後才失敗」才降級為 in-band event |
| 串流只回緩衝內容、丟棄後續 | 已修：commit 後續傳，commit 前才可重試 |
| passthrough 繞過模型發布政策 | 已修：passthrough 也驗證 |
| CORS 反射任意 Origin | 已修：只認伺服器事實 |
| `X-Armor-Admin: 1` 常數即可管理 | 已修：需真憑證 |
| 金鑰檔損壞即靜默歸零 | 已修：隔離成 `.corrupt-*` 並告警 |
