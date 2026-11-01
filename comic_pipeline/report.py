from html import escape
from pathlib import Path


def make_report(output: Path, summary):
    sections = []
    for page in summary["pages"]:
        pid = page["id"]
        prefix = "pages/" + pid + "/"
        figures = []
        for name, label in [("original.png", "原圖"), ("detected.png", "文字框／閱讀順序"),
                            ("mask.png", "消字遮罩"), ("cleaned.png", "修復後"),
                            ("cleaned-refined.png", "氣泡清理後"),
                            ("translated.png", "繁體中文"), ("reference.png", "沉浸式翻譯對照")]:
            if name == "reference.png" and not page.get("has_reference"):
                continue
            if (output / prefix / name).is_file() and (page["status"] != "error" or name == "original.png"):
                figures.append(f'<figure><a href="{prefix}{name}" target="_blank"><img loading="lazy" src="{prefix}{name}" alt="{label}"></a><figcaption>{label}</figcaption></figure>')
        rows = "".join(f'<tr><td>{escape(b["id"])}</td><td>{escape(b.get("source_text", ""))}</td><td>{escape(b.get("translation", ""))}</td></tr>' for b in page.get("blocks", []))
        notes = "".join(f"<li>{escape(note)}</li>" for note in page.get("warnings", []))
        if page.get("error"):
            notes += "<li>" + escape(page["error"]) + "</li>"
        status = {"complete": "處理完成 · 譯文待人工檢查", "needs_review": "有待檢查項目", "error": "處理失敗"}.get(page["status"], page["status"])
        sections.append(f'<section id="{pid}"><h2>{escape(page["filename"])}</h2><span class="status">{escape(status)}</span><div class="gallery">{"".join(figures)}</div><ul>{notes}</ul><table><thead><tr><th>文字框</th><th>日文 OCR</th><th>繁體中文</th></tr></thead><tbody>{rows}</tbody></table><p><a href="{prefix}page.json">頁面資料</a> · <a href="{prefix}blocks.json">文字與座標</a></p></section>')
    provider = escape(summary["translator"]["provider"])
    ocr = escape(summary.get("ocr_model", "unknown"))
    render_style = {"outline": "保留畫面、對比描邊"}.get(summary.get("render_style"), "初版嵌字")
    if summary.get("cleanup_profile") == "bubble-refined":
        render_style = "氣泡消字／排字修正版 · " + render_style
    mode = "文字裁圖模式（明確略過偵測）" if summary.get("input_mode") == "text-crop" else "自動文字偵測模式"
    note = "Google 網頁翻譯基準；逐框翻譯，尚未加入作品記憶。" if provider == "google-web" else "尚未加入跨章節作品記憶。"
    html = f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>漫畫翻譯 · 第一階段驗證</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f2ef;color:#262b2c;font:16px/1.65 "Microsoft JhengHei",sans-serif}}main{{max-width:1440px;margin:auto;padding:32px}}header{{margin-bottom:32px}}h1{{font-size:30px;margin-bottom:8px}}.sub{{color:#576467}}section{{background:white;padding:24px;margin:24px 0;border-radius:12px;border:1px solid #dedfdc}}h2{{display:inline;font-size:20px}}.status{{margin-left:16px;color:#426c60;font-size:13px}}.gallery{{display:flex;gap:16px;overflow-x:auto;padding:20px 0}}figure{{margin:0;min-width:200px;flex:1}}figure img{{height:300px;width:100%;object-fit:contain;background:#e9e8e4;border:1px solid #ddd}}figcaption{{font-size:14px;text-align:center;color:#53615e}}table{{border-collapse:collapse;width:100%;table-layout:fixed}}th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left;word-break:break-word;white-space:pre-wrap}}th:first-child,td:first-child{{width:85px}}th{{font-size:13px;color:#59655e}}a{{color:#256d5b}}li{{color:#a24c27}}@media(max-width:700px){{main{{padding:16px}}section{{padding:16px}}figure{{min-width:230px}}}}</style>
<main><header><div class="sub">PHASE 01 · JAPANESE → TRADITIONAL CHINESE</div><h1>漫畫翻譯管線驗證</h1><p>{escape(summary["book"])} · {len(summary["pages"])} 張圖片 · {provider} · OCR: {ocr}</p><p>{mode} · 嵌字：{render_style}</p><p class="sub">{note} 點擊圖片可查看原尺寸。這份報告展示處理結果，不代表已通過整章品質驗收。</p><p><a href="run.json">執行紀錄</a> · <a href="project.sqlite3">SQLite 資料庫</a></p></header>{"".join(sections)}</main></html>'''
    (output / "index.html").write_text(html, encoding="utf-8")
