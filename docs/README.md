[README](../README.md) · [部署](DEPLOYMENT.md) · [架構](ARCHITECTURE.md) · [使用說明書](MANUAL.md) · [免費託管](HOSTING.md) · [安全](../SECURITY.md)

---

# 文件索引

| 文件 | 內容 | 讀者 |
|---|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | 四種部署佈局、systemd、對外、免費託管摘要 | 要架站的人 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 請求生命週期、模組邊界、重試與清理設計 | 要改程式的人 |
| [MANUAL.md](MANUAL.md) | 完整使用說明書：檔案放哪、架構、設定、疑難排解 | 使用者 |
| [HOSTING.md](HOSTING.md) | 免費託管的實查結果與步驟 | 想放上雲的人 |

## 建議閱讀順序

**只想用** → [MANUAL.md](MANUAL.md) 第一到六章
**要架站** → [DEPLOYMENT.md](DEPLOYMENT.md) 挑一個佈局
**要看懂設計** → [ARCHITECTURE.md](ARCHITECTURE.md)
**卡住了** → MANUAL 第十章「疑難排解」

## 兩個最常見的誤解

**「Base URL 填供應商網址」** — 不是。填**代理**的網址，結尾 `/v1`。
供應商網址是代理自己去連的，客戶端看不到。

**「API Key 填我的 sk-…」** — 不是。填**代理發的** `triad-…`。
上游金鑰留在代理上，這就是為什麼可以安全地把代理金鑰給別人。

---

Made by yuangeluyou.com :)
