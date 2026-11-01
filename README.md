# 本機漫畫翻譯

Chrome 擴充套件與本機 Python 後端，支援 BookWalker、pixivコミック，以及自行選取的本機圖片。預設將日文翻譯為繁體中文。

目前版本：**0.22.2**。這份倉庫從整理後的程式建立獨立歷史，不含私人開發倉庫的提交歷史、漫畫範例、閱讀資料庫或模型權重。內建示範為專案自製的幾何排版測試圖。

## 功能

- 本機文字偵測、OCR、氣泡清理、消字、直排與描邊。
- Google AI Studio／Vertex AI 連線、Gemini 模型切換、暫時錯誤重試與可選顯式快取。
- 使用者選擇啟用的作品資料、逐頁閱讀記憶與累積故事大綱，本機 SQLite 管理。
- 支援閱讀器自動預翻後續 1–5 個畫面，以及清除快取、釋放 GPU。

## Windows 安裝

需要 Windows、Chrome、Git、Python **3.12**。首次安裝會下載 Python 依賴、固定版本的 Comic Translate 及預設偵測／OCR／LaMa 模型。

```powershell
git clone https://github.com/ccu412430024/Comic-translator-public.git
cd Comic-translator-public
.\scripts\setup.ps1 -Python 'C:\path\to\Python312\python.exe'
.\scripts\start-reader.ps1
```

開啟 <http://127.0.0.1:8765/>，設定 Gemini 連線。Chrome 開啟 `chrome://extensions`，啟用開發人員模式，載入本專案 `chrome-extension` 資料夾。也可在本機設定頁下載套件 ZIP。

自動啟動本機服務為另行選擇安裝：

```powershell
.\scripts\install-reader-autostart.ps1
```

預設環境提供 CPU 推論。DirectML 環境可用 `scripts/setup-lama-gpu.ps1` 安裝；自動 GPU 策略沿用已測硬體判定，其他顯示卡需自行驗證。額外 CTD、AOT、PP-OCRv6、NAR、Paddle 或 SAM 模型與隔離環境沒有預載，未準備的選項會停用。公開版本保留相應介面與執行程式，不宣稱所有選配模型在新電腦都已完成安裝。

## 資料與連線

閱讀圖像與譯圖在服務運作時暫存；自行建立的作品資料、原文、譯文及記憶保存於本機 `.runtime/`。Google AI Studio 金鑰只留在服務記憶體；Vertex AI 使用 Google Cloud CLI 的本機 ADC 登入。

Gemini 翻譯會傳送圖片、OCR 文字與啟用的作品上下文到 Google；免金鑰 Google 測試引擎傳送 OCR 文字。啟用顯式快取後，相關固定上下文會保存於 Google。費用與可用性依使用者帳戶；不提供共用金鑰。

## 檢查

```powershell
python scripts/check_public_release.py
python scripts/security_scan.py install
python scripts/security_scan.py install-hooks
python scripts/security_scan.py history
```

本機測試使用自製圖片或合成資料，不需真實翻譯金鑰或下載模型：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_reader.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_reader_processing.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_library.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_work_memory.py -v
node --test tests/extension.test.mjs tests/content-overlay.test.mjs
```

## 授權

本專案自製程式碼目前**未指定開源授權**；公開原始碼不代表額外授予修改、再散布或商用許可。第三方程式、模型及服務各自適用其原授權／條款，詳見 [THIRD_PARTY.md](THIRD_PARTY.md)。使用者應自行確認所處理圖像與資料的權利。

發布內容與同步方式見 [DISTRIBUTION.md](DISTRIBUTION.md)，憑證與掃描見 [SECURITY.md](SECURITY.md)。
