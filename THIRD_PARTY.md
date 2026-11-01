# 第三方來源與授權

本倉庫沒有對自製程式碼指定開源授權。以下第三方元件仍適用其原授權；本文件不更改或取代原 LICENSE、NOTICE、模型卡及服務條款。

## 預設後端

| 元件 | 來源 | 上游標示 |
|---|---|---|
| Comic Translate | [固定 commit 8977b91a4f7a40c3917c5a268e9e7d78e1d818da](https://github.com/ogkalu2/comic-translate/tree/8977b91a4f7a40c3917c5a268e9e7d78e1d818da) | Apache-2.0；由安裝程式另行取得，保留原授權 |
| 偵測模型 | [ogkalu/comic-text-and-bubble-detector](https://huggingface.co/ogkalu/comic-text-and-bubble-detector) | Apache-2.0 |
| Manga OCR | [ogkalu/manga-ocr-onnx](https://huggingface.co/ogkalu/manga-ocr-onnx)、[manga-ocr-mobile](https://huggingface.co/ogkalu/manga-ocr-mobile) | 依固定模型卡、原 Manga-OCR 與轉換版條款 |
| LaMa | [ogkalu/lama-manga-onnx-dynamic](https://huggingface.co/ogkalu/lama-manga-onnx-dynamic) | 模型頁標示 Apache-2.0 |

預設模型下載路徑與 SHA-256 由上述 Comic Translate 的固定 registry 提供。權重、vendor checkout、模型附帶文件不在本倉庫 Git 中，安裝後應保留其授權。

## 選配執行介面

- CTD：[dmMaze/comic-text-detector](https://github.com/dmMaze/comic-text-detector)，程式 GPL-3.0；公開權重來源為 manga-image-translator 的發布包。程式與權重條件需分別確認。
- BallonsTranslator：[固定上游 fa3e1374b4ca7c040a1a04effb96c95aac2a5e5c](https://github.com/dmMaze/BallonsTranslator/tree/fa3e1374b4ca7c040a1a04effb96c95aac2a5e5c)，GPL-3.0；本倉庫沒有附其 checkout。包裝器可供另外準備環境時使用。
- AOT：[ogkalu/aot-inpainting](https://huggingface.co/ogkalu/aot-inpainting)、[AOT-GAN](https://github.com/researchmm/AOT-GAN-for-Inpainting)。原模型、轉換版與漫畫權重應按各自原始聲明確認；本文件不作額外再授權。
- PP-OCRv6：[PaddlePaddle/PP-OCRv6_medium_det_onnx](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det_onnx)，模型頁標示 Apache-2.0；PaddleOCR 後處理另保留 Apache-2.0 授權。
- NAR：[muscgab/manga-ocr-nar-small](https://huggingface.co/muscgab/manga-ocr-nar-small)，**程式 Apache-2.0、模型權重 CC BY-NC-SA 4.0**；非商業等權重限制仍適用。
- Paddle OCR：[jzhang533/PaddleOCR-VL-For-Manga](https://huggingface.co/jzhang533/PaddleOCR-VL-For-Manga)，來源頁標示 Apache-2.0；隔離工作程序可能載入額外官方模型程式。
- SAM 2.1：[facebookresearch/sam2](https://github.com/facebookresearch/sam2)，依官方 Apache-2.0 等授權檔，需另外準備環境與權重。
- Jieba：[fxsjy/jieba](https://github.com/fxsjy/jieba)，MIT；外部詞典未附本倉庫。
- DBNet 的獨立介面可載入 [manga-image-translator](https://github.com/zyddnys/manga-image-translator) 上游，GPL-3.0；預設安裝不準備該研究環境。

## 其他依賴與素材

Pillow、NumPy、ONNX Runtime、OpenCV、Google Gen AI SDK、google-auth 等依其套件授權。PySide6 Essentials 有 LGPL／GPL／商業授權條款；Qt 原版排字功能使用該依賴。Gitleaks CLI 使用官方固定版本與雜湊，只下載到本機忽略的執行目錄，原專案為 [Gitleaks](https://github.com/gitleaks/gitleaks)。

系統 CJK 字型在執行時使用，沒有附字型檔。僅附 `samples/smoke/01.png`、`02.png`，由本專案以幾何版面及一般短句產生；不含實際漫畫分鏡。研究程式可選擇下載的公開 OCR 圖片並未附在此倉庫，公開可讀不等於可自由再散布。

Google API 與閱讀平台各自有服務條款。平台及模型名稱用於相容性與依賴識別，不代表官方背書。
