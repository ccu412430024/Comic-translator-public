"""Named processing presets shared by the reader API and its model adapters."""
from pathlib import Path
from importlib.util import find_spec

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT/".runtime/upgrade-models"
CLEANING = {
    "standard": "改良版：LaMa＋氣泡清理",
    "comic-original": "Comic Translate 原方案（AOT）",
    "ctd-lama": "CTD 遮罩＋LaMa",
    "ctd-aot": "CTD 遮罩＋AOT",
    "hybrid-lama": "CTD 混合遮罩＋LaMa",
    "hybrid-aot": "CTD 混合遮罩＋AOT",
}
LAYOUTS = {"contour": "輪廓排版＋繁體斷詞", "rectangle": "基礎矩形排版",
           "comic-original": "Comic Translate 原版 Qt 排版", "sam": "SAM 2.1＋輪廓排版"}


class ProcessingError(RuntimeError):
    """Fixed, credential-free errors suitable for the local reader UI."""


def capabilities():
    ctd = (ASSETS/"ctd.onnx").is_file()
    aot = (ASSETS/"aot.onnx").is_file()
    sam = all(path.exists() for path in [ROOT/".venv-upgrades/Scripts/python.exe",
              ASSETS/"sam2.1_hiera_tiny.pt", ROOT/"vendor/sam2/sam2"])
    original_deps = all(find_spec(name) is not None for name in ("PySide6", "jwt", "keyring", "msgpack"))
    original_aot = aot or (ROOT/".runtime/ComicTranslate/models/inpainting/aot.onnx").is_file()
    cleaning = {}
    for key in CLEANING:
        available = (original_deps and original_aot if key == "comic-original" else
                     key == "standard" or ctd and (not key.endswith("aot") or aot))
        cleaning[key] = dict(available=available, reason="" if available else
                            "請先安裝原版排版依賴並準備 AOT 模型" if key == "comic-original" else "請先準備 CTD／AOT 模型")
    layout = {}
    for key in LAYOUTS:
        available = original_deps if key == "comic-original" else key != "sam" or sam
        layout[key] = dict(available=available, reason="" if available else
                          "請先安裝 requirements.txt 的原版排版依賴" if key == "comic-original" else
                          "請先準備 SAM 2.1 模型及 .venv-upgrades 環境")
    from .ocr_backend import capabilities as ocr_capabilities
    return dict(cleaning=cleaning, layout=layout, ocr=ocr_capabilities())
