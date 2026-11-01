"""Generate original synthetic layout fixtures with known Japanese dialogue.

These are engineering smoke fixtures, not actual manga or evidence of manga quality.
"""
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def main():
    out = ROOT / "samples" / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype("C:/Windows/Fonts/YuGothM.ttc", 34)
    truth = {}
    fixtures = [
        [("学校に行こう。", (560, 80, 850, 470), True, "white"),
         ("今日はいい天気だね。", (70, 90, 450, 300), False, "white"),
         ("明日もここで待っている。", (480, 710, 850, 1050), False, "white")],
        [("これは何ですか？", (570, 70, 850, 470), True, "white"),
         ("約束を忘れないで。", (50, 140, 440, 350), False, "black"),
         ("ありがとう。また会おう。", (470, 730, 850, 1050), False, "white")],
    ]
    for page_no, bubbles in enumerate(fixtures, 1):
        image = Image.new("RGB", (900, 1200), "#eeeeee")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 880, 570), fill="white", outline="black", width=4)
        draw.rectangle((20, 610, 880, 1180), fill="white", outline="black", width=4)
        for x in range(30, 880, 24):
            draw.line((x, 630, x+150, 1170), fill="#d5d5d5", width=1)
        entries = []
        for text, box, vertical, background in bubbles:
            draw.ellipse(box, fill=background, outline="black", width=3)
            color = "white" if background == "black" else "black"
            if vertical:
                x = (box[0]+box[2])//2-17
                y = box[1]+35
                for char in text:
                    draw.text((x, y), char, font=font, fill=color)
                    y += 42
                bbox = [x, box[1]+35, x+38, y]
            else:
                split = max(1, len(text)//2)
                value = text[:split]+"\n"+text[split:]
                bounds = draw.multiline_textbbox((0, 0), value, font=font, spacing=8)
                x = (box[0]+box[2]-(bounds[2]-bounds[0]))/2-bounds[0]
                y = (box[1]+box[3]-(bounds[3]-bounds[1]))/2-bounds[1]
                draw.multiline_text((x, y), value, font=font, fill=color, spacing=8)
                bbox = list(draw.multiline_textbbox((x, y), value, font=font, spacing=8))
            entries.append({"source_text": text, "bbox": bbox, "vertical": vertical})
        name = f"{page_no:02}.png"
        image.save(out / name)
        truth[name] = entries
    (out / "ground-truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
