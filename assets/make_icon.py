"""由 assets/app_icon_source.png 生成 app_icon.ico（窗口/任务栏）与 app_icon.png（界面内品牌区）。

换图后运行：python assets/make_icon.py
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "app_icon_source.png")
DST = os.path.join(HERE, "app_icon.ico")
PNG = os.path.join(HERE, "app_icon.png")      # 界面内品牌区用，Tk 的 PhotoImage 读不了 ico
SIZES = (16, 24, 32, 48, 64, 128, 256)
ZOOM = 1.10        # 源图四周有一圈浅色外发光，放大后让圆角方块铺满画布再裁
RADIUS = 0.22      # 图标圆角占边长比例


def master(path: str = SRC, size: int = 1024) -> Image.Image:
    img = Image.open(path).convert("RGB")
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    big = size * 2
    img = img.crop((left, top, left + side, top + side)).resize((big, big), Image.LANCZOS)

    # 放大后再居中裁回，把源图边缘的外发光推到画布外
    crop_side = int(round(big / ZOOM))
    off = (big - crop_side) // 2
    img = img.crop((off, off, off + crop_side, off + crop_side)).resize((big, big), Image.LANCZOS)

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1],
                                           radius=int(big * RADIUS), fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    if not os.path.isfile(SRC):
        print(f"missing {SRC}", file=sys.stderr)
        return 1
    base = master()
    base.save(DST, sizes=[(s, s) for s in SIZES])
    base.resize((256, 256), Image.LANCZOS).save(PNG)
    print(f"wrote {DST} + {PNG}  sizes={SIZES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
