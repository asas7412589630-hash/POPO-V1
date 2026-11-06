[README](../README.md) · [部署](DEPLOYMENT.md) · [架構](ARCHITECTURE.md) · [使用說明書](MANUAL.md) · [免費託管](HOSTING.md) · [安全](../SECURITY.md)

---

# 免費託管部署指南

**目標**：把 Cinder Server 放上免費主機，自用不公開，且不留任何記錄。

---

## 一、先講結論：免費託管的現實

| 平台 | 免費額度 | 常駐 | 台灣/亞洲節點 | 適合度 |
|---|---|---|---|---|
| **Oracle Cloud Always Free** | 4 OCPU / 24GB RAM **永久免費** | ✅ 真 VM | ✅ 東京/首爾/新加坡 | ⭐⭐⭐⭐⭐ |
| **Fly.io** | 試用額度，之後按量 | ✅ | ✅ 東京/新加坡 | ⭐⭐⭐ |
| **Render** | 750h/月 但**15 分鐘沒請求就休眠** | ❌ 會睡 | ❌ 只有美歐 | ⭐⭐ |
| **Railway** | $5 試用，之後收費 | ✅ | ✅ | ⭐⭐ |
| **Koyeb** | **已取消免費層** | — | — | ❌ |
| **Google Cloud Run** | 每月 200 萬請求免費 | ✅ scale-to-zero | ✅ | ⭐⭐⭐（冷啟動慢）|
| **Hugging Face Spaces** | 免費但公開、會睡 | ❌ | ❌ | ❌ |
| **Replit** | 免費會公開程式碼 | ❌ | — | ❌ 絕對不要 |

**唯一真正「永久免費 + 常駐 + 亞洲節點」的是 Oracle Cloud Always Free。**
其他都是「有條件免費」或「會休眠」。

---

## 二、Oracle Cloud Always Free（推薦）

### 2.1 註冊

1. https://cloud.oracle.com/free
2. 需要信用卡驗證（**不會扣款**，只是身分驗證）
3. 選 **Home Region 選東京或首爾**（註冊後不能改）

### 2.2 開一台 VM

Console → Compute → Instances → **Create Instance**

| 欄位 | 選什麼 |
|---|---|
| Image | **Ubuntu 22.04** 或 Oracle Linux 9 |
| Shape | **VM.Standard.A1.Flex**（ARM，免費 4 OCPU / 24GB） |
| OCPU | 1（夠用，省額度） |
| Memory | 6 GB |
| Boot volume | 50 GB |
| **Public IP** | **不要勾**（省得對外） |
| SSH key | 上傳你的公鑰 |

> **ARM 實例常缺貨**。缺貨就換區域試，或等幾小時。x86 的 `VM.Standard.E2.1.Micro` 也免費但只有 1GB RAM。

### 2.3 連上去

用 **Cloud Shell**（Console 右上角 `>_`）或 SSH：

```sh
ssh ubuntu@<private-ip>          # 用 Cloud Shell 從內部連
ssh -i key.pem ubuntu@<public-ip> # 有公網 IP 時
```

沒有公網 IP 也能用 **OCI Bastion** 或 Cloud Shell 的內部網路。

### 2.4 裝環境

```sh
sudo apt update
sudo apt install -y python3 unzip
python3 --version    # 需要 3.9+
```

### 2.5 上傳並啟動

```sh
# 在本機
scp cinder-server-v2.zip ubuntu@<host>:~

# 在伺服器
unzip cinder-server-v2.zip -d cinder-server
cd cinder-server
chmod +x triad-up triad-tunnel
```

**用無痕模式啟動**（見第三章）：

```sh
ARMOR_EPHEMERAL=1 ./triad-up
```

### 2.6 用 systemd 常駐

```sh
sudo tee /etc/systemd/system/cinder.service > /dev/null <<'EOF'
[Unit]
Description=Cinder proxy (ephemeral)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/cinder-server
Environment=ARMOR_EPHEMERAL=1
Environment=ARMOR_BIND=127.0.0.1
Environment=TRIAD_PORT=8412
ExecStart=/home/ubuntu/cinder-server/run-proxy.sh
Restart=always
RestartSec=3
# 收緊權限：只給它需要寫的
ReadWritePaths=/home/ubuntu/cinder-server/logs
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cinder
sudo systemctl status cinder
```

---

## 三、無痕模式（ARMOR_EPHEMERAL=1）

### 3.1 它關掉什麼

| 項目 | 關閉後 |
|---|---|
| 請求日誌 `requests.jsonl` | 不寫 |
| 逐客戶端記錄 `logs/<id>/*.log` | 不寫 |
| 逐回合 trace `runtime/trace/` | 不寫 |
| 存檔 `state/` `artifacts/` | 不寫 |
| 用量帳本 `last_usage.json` | 只在記憶體 |
| 帳戶 `proxy/accounts.json` | 整層不載入 |
| 請求快照 `last_request.json` 等 | 不寫 |
| 指紋 debug `fp_debug.jsonl` | 不寫 |
| setup token | 只存記憶體 |

### 3.2 它保留什麼

| 項目 | 為什麼 |
|---|---|
| 上游金鑰（讀取） | 是**輸入**不是記錄，不寫回 |
| 破甲 `blocks/` | 是程式的一部分 |
| 控制台 | 需要它來管金鑰 |

### 3.3 實測

```
發 3 個含可辨識字串的請求 → 全盤 grep 該字串 → 命中 0
落檔檢查 → logs/ state/ runtime/trace/ artifacts/ proxy/logs/ 全部 0 檔
```

---

## 四、「有些節點不信任網站」怎麼解

這是**出網 IP 被 CDN/WAF 列黑**，跟你的金鑰無關。

### 4.1 症狀

```
HTTP 403 + HTML（不是 JSON）
<title>Attention Required! | Cloudflare</title>
Ray ID: 8a1b2c3d...
```

或 429 / 503，但 body 是 HTML 頁面。

### 4.2 已經幫你做的（程式層）

**① 自動改寫 bot UA**

代理原本會把客戶端的 UA 原樣轉給上游。用 `python-urllib`、`curl`、`Go-http-client`
這些字串是每個 WAF 的黑名單——請求根本到不了模型。

現在會自動換成正常瀏覽器 UA：

```python
_BOT_UA_RE = re.compile(r"^(python-?(urllib|requests|httpx)|curl/|wget/|java/|go-http-client)", re.I)
# 命中就換成 Chrome UA；你自己設的 UA 不會被動
```

**② 補上 WAF 會看的標頭**

```
Accept-Language: en-US,en;q=0.9,zh-TW;q=0.8
Accept: application/json, text/event-stream, */*
```

**③ 明確區分「被邊緣擋」與「金鑰錯」**

```
upstream.edge_blocked   403  被 CDN/WAF 擋住，請求沒到模型
                       問題不在金鑰、不在破甲。改 UA / 換域名 / 搬 IP。
```

### 4.3 你可以調的

**換自訂 UA**

```sh
ARMOR_UPSTREAM_UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15" \
./triad-up
```

**走得通的幾條路**

| 方法 | 說明 |
|---|---|
| **用官方域名** | `api.openai.com` 很少擋；`xxx.workers.dev` 這類被擋機率高 |
| **換供應商** | 同一模型常有不同中轉，換一家 IP 乾淨的 |
| **住宅 IP 代理** | 最有效但通常要錢 |
| **自架上游** | Ollama / vLLM 跑在自己機器，完全沒 WAF |

### 4.4 誠實的話

**如果你的免費主機 IP 已經被目標網站封鎖，程式層救不了。**

WAF 看的是 IP 信譽、TLS 指紋、行為模式。免費共享主機的 IP 段常被整段封。
UA 改寫只對「只看 UA 的寬鬆 WAF」有效。

真正解法順序：
1. 換有乾淨 IP 的供應商（最快）
2. 換主機地區（Oracle 東京 → 有時換成首爾就通）
3. 住宅 IP（要錢）
4. 自架上游（最徹底，但要 GPU）

---

## 五、不對外開放（自用）

### 5.1 完全不出網

```sh
ARMOR_BIND=127.0.0.1 ./triad-up
```

只監聽本機。要用的話靠 **SSH 隧道**：

```sh
# 在你的筆電上
ssh -N -L 8412:127.0.0.1:8412 ubuntu@<伺服器>
```

然後本機 `http://127.0.0.1:8412/v1` 就是那台伺服器的代理。

**這是最安全的方式**——伺服器不開任何對外埠。

### 5.2 只給自己人（Cloudflare Tunnel + Access）

如果要從手機直接連：

```sh
# 伺服器上
cloudflared tunnel login
cloudflared tunnel create cinder
cloudflared tunnel run --url http://127.0.0.1:8412 cinder
```

然後在 Cloudflare Zero Trust 設 **Access Policy**：只允許你的 email。

這樣網址雖然公開，但沒登入的人連不到。

---

## 六、免費額度注意事項

| 專案 | 提醒 |
|---|---|
| Oracle ARM 實例閒置回收 | 長時間 0 CPU 可能被回收。加個 cron 每小時 curl 一次自己 |
| Fly.io 免費額度 | 用完會停機，不是無限 |
| Cloud Run 冷啟動 | 第一個請求可能等 10-20 秒 |
| Render 免費 | 15 分鐘無請求就睡，下個請求要等 50 秒醒來 |

**防回收（Oracle）**：

```sh
# crontab -e
0 * * * * curl -s http://127.0.0.1:8412/healthz > /dev/null
```

---

## 七、一頁速查

```sh
# 最小可用部署（Oracle ARM，無痕，不對外）
sudo apt install -y python3 unzip
unzip cinder-server-v2.zip -d ~/cinder-server
cd ~/cinder-server && chmod +x triad-up triad-tunnel
ARMOR_EPHEMERAL=1 ./triad-up

# 從本機連（SSH 隧道）
ssh -N -L 8412:127.0.0.1:8412 ubuntu@伺服器

# 本機設定
Base URL: http://127.0.0.1:8412/v1
API Key:  （控制台「金鑰」頁建一把）

# 被 WAF 擋時
ARMOR_UPSTREAM_UA="Mozilla/5.0 ..." ./triad-up
```

---

Made by yuangeluyou.com :)
