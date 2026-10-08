#!/usr/bin/env python3
"""从仓库已有的 modules/m9_web/web/icon-512.png 生成 Android mipmap 图标。

用法（在仓库根目录执行）：
    python android/tools/make_icons.py

不依赖任何外部图片，输出到 android/app/src/main/res/mipmap-*/ 。
"""

import os
import sys

from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "modules", "m9_web", "web", "icon-512.png")
RES = os.path.join(REPO_ROOT, "android", "app", "src", "main", "res")

DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

NAMES = ("ic_launcher.png", "ic_launcher_round.png")


def main() -> int:
    if not os.path.isfile(SRC):
        print("missing source icon: %s" % SRC, file=sys.stderr)
        return 1

    src = Image.open(SRC).convert("RGBA")
    print("source: %s %s" % (src.size, src.mode))

    for density, px in DENSITIES.items():
        out_dir = os.path.join(RES, "mipmap-%s" % density)
        os.makedirs(out_dir, exist_ok=True)
        scaled = src.resize((px, px), Image.LANCZOS)
        for name in NAMES:
            scaled.save(os.path.join(out_dir, name), "PNG", optimize=True)
        print("mipmap-%s: %dx%d" % (density, px, px))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
