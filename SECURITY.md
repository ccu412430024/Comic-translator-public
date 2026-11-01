# 金鑰與憑證保護

Gemini API 金鑰只交給本機服務，不提交至 Git。Vertex AI 使用本機 Google ADC 登入，憑證由 Google Cloud 工具保存在使用者設定目錄；不要複製到專案、SQLite、報告或測試資料。金鑰掃描與 CI 不需要 Gemini／GCP 金鑰。

## 第一次設定本機掃描

在專案根目錄執行，需 Python 3 與 Git：

```powershell
.venv/Scripts/python.exe scripts/security_scan.py install
.venv/Scripts/python.exe scripts/security_scan.py install-hooks
```

第一步下載固定版本的官方 Gitleaks CLI，驗證封存檔與執行檔 SHA-256，只保存在忽略的 `.runtime/security-tools/`；第二步只設定這個儲存庫的 `core.hooksPath`，啟用提交及推送前掃描。掃描時再次驗證執行檔；工具缺失或被改動會阻止提交，不會默默略過檢查。安裝程式遇到既有 hook 設定會停止，保留原設定。

Git 不會自動啟用新 clone 中的 hooks；換電腦或重新 clone 後需要再執行上述設定。支援 Windows x64 與 Linux x64，不需 GPU、Go、Docker 或應用程式模型。

## 手動檢查與公開前檢查

```powershell
.venv/Scripts/python.exe scripts/security_scan.py staged
.venv/Scripts/python.exe scripts/security_scan.py worktree
.venv/Scripts/python.exe scripts/security_scan.py history
```

- `staged`：檢查暫存變更，供 `pre-commit` 使用；檢查的是 Git index。
- `worktree`：檢查已追蹤與未忽略的本機檔案，包括未暫存內容；不走訪忽略的模型、憑證或執行期目錄。
- `history`：檢查本機所有可達分支與標籤的完整歷史，供 `pre-push` 使用；淺層 clone 會失敗，不會假裝已掃完。掃描範圍以本機已取得的 refs 為準。

公開前先取得要公開的所有分支與標籤，再執行 `history` 與 `worktree`。目前檔案乾淨不代表過去提交乾淨。掃描不能保證找出所有憑證，也不會檢查 GitHub Issues、附件、聊天紀錄或已上傳的工作日誌。

退出碼：`0` 為本次掃描未發現問題，`1` 為發現疑似憑證或敏感檔案，`2` 為工具缺失／掃描未完成。兩種失敗都會阻止本機 hook 與 CI 通過。

## 規則與輸出

`security/gitleaks-config.toml` 延伸 Gitleaks 內建規則，另涵蓋此專案使用的 Google `AQ.` 格式與 OAuth refresh token。敏感檔名政策可攔截誤用 `git add -f` 加入的 `.env`、ADC、service-account JSON、私鑰與 `.runtime/`；範例環境檔仍須使用佔位值。

目前只有 Chrome manifest 的**確切公開 SPKI 金鑰＋確切檔案路徑＋指定規則**例外。此值用來固定擴充套件識別碼，不是帳戶憑證。沒有整個略過 `tests/`、`reports/` 或 manifest 檔案，也不採用把現有警報整批隱藏的 baseline；行內 `gitleaks:allow` 與專案 `.gitleaksignore` 不會繞過本工具。

掃描器使用完全遮蔽模式，外層僅輸出規則、檔案、行號與提交 ID；不輸出匹配內容、金鑰、原始行或任意描述。原始掃描輸出被截住，暫存報告用後刪除。可保存的去識別報告位於忽略的 `.runtime/security-scans/`，也不要將私人檔名等資料貼到公開 issue。

## GitHub Actions

`.github/workflows/secret-scan.yml` 在 push、PR、手動觸發與每週排程執行，採用 GitHub 提供的臨時 Linux runner、完整歷史、固定 SHA 的 checkout，以及經 SHA-256 驗證的 Gitleaks CLI。權限只有 `contents: read`，不保留 checkout 登入憑證、不注入翻譯金鑰、不上傳原始掃描報告，也不使用 `pull_request_target` 執行 PR 程式。

這個配置直接使用開源 CLI，不依賴 Gitleaks Action 的組織授權，也不依賴付費 GitHub Secret Protection；GitHub Actions 本身仍受帳號執行額度與政策限制。定時觸發需工作流程已位於預設分支。

Actions 在內容推送後才執行。要強制阻止有問題的合併，需另在 GitHub branch rules 將 `Secrets and repository hygiene` 設為必要檢查；本機 hooks 也可能被 `--no-verify` 繞過。支援的帳號應另外啟用 GitHub 原生 push protection。私人儲存庫的原生保護取決於 GitHub 帳號、方案與授權；本專案不會自動開通付費功能或更改公開狀態。

## 發現實際洩漏時

先在金鑰提供者撤銷／輪替憑證，再移除程式中的內容，確認 Git 歷史、工作日誌與附件是否也包含同一憑證。只刪除目前檔案或新增 `.gitignore` 不會移除歷史提交。需要改寫歷史時，先備份並確認受影響分支與協作者；不自動 force-push，也不要用公開 issue 貼上實際金鑰。

官方來源：[Gitleaks](https://github.com/gitleaks/gitleaks)、[GitHub 推送保護](https://docs.github.com/en/code-security/concepts/secret-security/push-protection)、[Actions 安全設定](https://docs.github.com/en/actions/reference/security/secure-use)。
