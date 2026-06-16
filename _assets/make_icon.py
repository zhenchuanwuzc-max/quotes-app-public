#!/usr/bin/env python3
"""
生成 quotes.app 的 Q 字图标
- 用 PIL 渲染 1024x1024 PNG（圆角矩形底 + 居中 Q）
- 调 sips/iconutil 转 .icns
- 风格：奶油暖白底 + 深蓝 Q 字
"""
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(SCRIPT_DIR, "icon_1024.png")
OUT_ICONSET = os.path.join(SCRIPT_DIR, "icon.iconset")
OUT_ICNS = os.path.join(os.path.dirname(SCRIPT_DIR), "icon.icns")

SIZE = 1024
RADIUS = 224  # macOS App 圆角半径约 22%

# 配色（奶油暖白 + 深蓝）
BG = (253, 252, 247)   # #fdfcf7 奶油暖白
INK = (31, 111, 235)   # #1f6feb 深蓝
SHADOW = (200, 200, 200)


def make_png():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角矩形底
    d.rounded_rectangle(
        [(0, 0), (SIZE, SIZE)],
        radius=RADIUS,
        fill=BG,
    )

    # 渲染 Q
    # 用衬线字体显得知识感更强；系统找不到衬线就用 PingFang
    font_path = None
    candidates = [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Baskerville.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            font_path = p
            break

    font_size = 720
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()

    text = "Q"
    bbox = d.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (SIZE - text_w) // 2 - bbox[0]
    # Q 字下半身有钩，往上偏一点视觉居中
    y = (SIZE - text_h) // 2 - bbox[1] - 30

    d.text((x, y), text, font=font, fill=INK)

    img.save(OUT_PNG, "PNG")
    print(f"✅ PNG: {OUT_PNG}")


def make_icns():
    """用 sips 生成各尺寸 PNG，iconutil 转 .icns"""
    if os.path.exists(OUT_ICONSET):
        subprocess.run(["rm", "-rf", OUT_ICONSET], check=False)
    os.makedirs(OUT_ICONSET)

    # macOS .iconset 标准 10 个尺寸
    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in sizes:
        out = os.path.join(OUT_ICONSET, name)
        subprocess.run(
            ["sips", "-z", str(size), str(size), OUT_PNG, "--out", out],
            check=True, stdout=subprocess.DEVNULL
        )

    subprocess.run(
        ["iconutil", "-c", "icns", OUT_ICONSET, "-o", OUT_ICNS],
        check=True
    )
    print(f"✅ ICNS: {OUT_ICNS}")
    # 清掉中间产物
    subprocess.run(["rm", "-rf", OUT_ICONSET], check=False)
    print(f"   (PNG 保留作预览：{OUT_PNG})")


if __name__ == "__main__":
    make_png()
    make_icns()
