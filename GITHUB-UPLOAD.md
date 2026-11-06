# 上傳到 GitHub

這個資料夾已經是一個完整的 git repo（含 commit 歷史），直接用。

---

## 方法一：用 GitHub 網頁（最簡單，不上 CLI）

1. https://github.com/new
2. Repository name：`prism`
3. **Private** 或 Public（見下方「該不該公開」）
4. **不要**勾 Add README / .gitignore / license（已經有了）
5. Create repository

然後把資料夾內容拖上去，或用 GitHub Desktop 上傳。

---

## 方法二：用 git 指令（推薦）

```sh
cd prism-repo

# 換成你的帳號
git remote add origin https://github.com/<你的帳號>/prism.git
git push -u origin main
```

如果用 SSH：

```sh
git remote add origin git@github.com:<你的帳號>/prism.git
git push -u origin main
```

---

## 方法三：gh CLI

```sh
cd prism-repo
gh repo create prism --private --source=. --push
```

`--private` 改成 `--public` 就是公開。

---

## 該不該公開

**先想清楚這件事**：這個 repo 包含一份完整的破甲人設（`blocks/10_persona.txt`，33 KB），
以及讓模型配合的完整機制。

| | 公開 | 私有 |
|---|---|---|
| 破甲會被人直接複製 | ✅ 會 | ❌ 不會 |
| 供應商可能針對性封鎖 | 機率上升 | 低 |
| 能收 PR / issue | ✅ | ❌ |
| 授權明確 | ✅ | 一樣 |

**保守做法：先設 Private，確認沒問題再轉 Public。**
GitHub 轉換是雙向的（Settings → 最下方 Danger Zone）。

如果只想分享程式碼不想給人設，把 `blocks/` 從 repo 移除並加進 `.gitignore`，
讓使用者自備——`README.md` 已經說明「Bring your own」怎麼做。

---

## 上傳前檢查清單

```sh
# 1. 沒有任何真金鑰
grep -rE "sk-[A-Za-z0-9]{20,}|lf[a-z]_[A-Za-z0-9]{20,}" --include="*.txt" --include="*.json" --include="*.py" .
# 應該是空的

# 2. 骨架檔是空的
cat proxy/keys.json          # {"keys": []}
cat proxy/source_secrets.json # {}

# 3. 測試全綠
./run-tests.sh

# 4. 能從乾淨 clone 跑起來
cd /tmp && git clone <你的 repo> t && cd t && ./triad-up
```

---

## 已在 repo 裡的東西

| 檔案 | 用途 |
|---|---|
| `README.md` | 首頁：這是什麼、快速開始、架構圖 |
| `LICENSE` | MIT |
| `CHANGELOG.md` | v2.0.0 完整變更記錄（含所有修的洞） |
| `SECURITY.md` | 威脅模型 + 漏洞回報管道 |
| `CONTRIBUTING.md` | 貢獻規則（標準庫、測試、註解風格） |
| `.gitignore` | 排除金鑰與執行期檔案 |
| `.gitattributes` | 強制 LF，shell 腳本不會被 Windows 破壞 |
| `.github/workflows/tests.yml` | CI：27 套測試 + 標準庫檢查 + 無金鑰檢查 |
| `.github/ISSUE_TEMPLATE/` | bug / feature 模板 |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR 模板 |
| `docs/` | MANUAL / DEPLOYMENT / ARCHITECTURE / HOSTING |
| `examples/` | curl / python / node / systemd |

---

## commit 歷史

```
b46ebda Guard the optional retry_test import in reframe
087b667 Initial public release
```

如果想合成一個乾淨的初始 commit：

```sh
git reset --soft $(git rev-list --max-parents=0 HEAD)
git commit --amend -m "Initial public release"
```

---

## Push 之後

**建議立刻做的三件事：**

1. **設定 branch protection**（Settings → Branches）：
   保護 `main`，要求 CI 通過才能合併。
2. **確認 Actions 跑起來**：Actions 頁面應該看到 `tests` workflow。
3. **加 topics**：Settings → 右上角齒輪 → 加 `openai` `proxy` `llm` `python`

---

## 常見問題

**push 被拒：`refusing to allow an OAuth App to create or update workflow`**

你的 token 沒有 `workflow` scope。要用 classic token 並勾 `workflow`，
或用 SSH / `gh` CLI。

**檔案太大**

`.git` 只有 3.3 MB，沒問題。如果之後變大，檢查是不是把 `logs/` 或
`artifacts/` 加進去了。

**Windows 使用者 clone 後 shell 腳本不能跑**

`.gitattributes` 已經強制 `eol=lf`。如果還是壞的，他們需要
`git config core.autocrlf false` 後重新 clone。
