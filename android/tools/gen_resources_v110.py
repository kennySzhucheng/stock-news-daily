#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Android 1.1.0 的资源文件（图标 + 布局 + 文案 + 主题）。

为什么用脚本而不是手写 XML：图标要同时产出
  · 自适应图标（API 26+ 的 mipmap-anydpi-v26 + 矢量前景）
  · 五种密度的 PNG（老启动器 / 系统缓存兜底）
  两者必须**同源同色**，脚本保证像素与矢量一致（同一组坐标按 512 画再缩放）。
用法：python android/tools/gen_resources_v110.py
"""
import os

from PIL import Image, ImageDraw

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RES = os.path.join(REPO, "android", "app", "src", "main", "res")

BRAND = "#1E4D7A"          # 与网页版主色一致（--accent #1e4d7a）
NAVY = "#123A5E"           # 图标底色
GOLD = "#E8B45A"           # 图标强调色
WHITE = "#FFFFFF"
MUTED = "#BFD4E8"          # 标题栏副标题
OFF = "#8C8579"            # 未选中 tab（偏暖，与站点的纸色系一致）
DIVIDER = "#E6E2D8"
ACCENT = "#2F80C0"
PROGRESS = "#E8B45A"


def w(rel, text):
    path = os.path.join(RES, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("  %-56s %5d B" % (rel, len(text.encode("utf-8"))))


def draw_icon(size=512, round_bg=False):
    """图标：深海军蓝底 + 金色圆环 + 三根上行白柱 + 金色圆点（“情报 + 上行”）。

    坐标按 512 设计，自适应图标的前景矢量用同一组比例（108 视口 = 512/4.74）。
    """
    s = size
    k = s / 512.0
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 底：圆角方（圆角 22%）或正圆
    if round_bg:
        d.ellipse([0, 0, s - 1, s - 1], fill=NAVY)
    else:
        r = int(112 * k)
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=NAVY)

    # 金色圆环，线宽 20/512
    ring_w = max(2, int(20 * k))
    pad = int(150 * k)
    d.ellipse([256 * k - pad, 256 * k - pad, 256 * k + pad, 256 * k + pad],
              outline=GOLD, width=ring_w)

    # 三根上行柱（圆头）
    bar_w = int(44 * k)
    bars = [(196, 250, 340), (256, 190, 340), (316, 130, 340)]
    for cx, top, bottom in bars:
        d.rounded_rectangle(
            [int(cx * k) - bar_w // 2, int(top * k),
             int(cx * k) + bar_w // 2, int(bottom * k)],
            radius=bar_w // 2, fill=WHITE)

    # 金色圆点（标记“最新/情报”）
    dot = int(24 * k)
    d.ellipse([int(316 * k) - dot, int(118 * k) - dot,
               int(316 * k) + dot, int(118 * k) + dot], fill=GOLD)
    return img


def main():
    print("== values ==")
    w("values/colors.xml", """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- 2026-10-19 v1.1.0：与网页版同一套色（站点 --accent #1e4d7a / 纸色 #f7f4ee） -->
    <color name="brand">%(brand)s</color>
    <color name="brand_dark">#163A5C</color>
    <color name="accent">%(accent)s</color>
    <color name="tab_inactive">%(off)s</color>
    <color name="tab_bar_bg">#FFFFFF</color>
    <color name="divider">%(divider)s</color>
    <color name="progress">%(progress)s</color>
    <color name="on_brand">#FFFFFF</color>
    <color name="brand_muted">%(muted)s</color>
    <color name="paper">#F7F4EE</color>
    <color name="ic_launcher_background">%(navy)s</color>
</resources>
""" % dict(brand=BRAND, accent=ACCENT, off=OFF, divider=DIVIDER,
           progress=PROGRESS, muted=MUTED, navy=NAVY))

    w("values/strings.xml", """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">股市情报</string>
    <string name="app_tagline">每日 08:10 / 15:40 自动更新</string>
    <string name="tab_daily">日报</string>
    <string name="tab_web">交互版</string>
    <string name="tab_review">复盘看板</string>
    <string name="action_refresh">刷新</string>
    <string name="action_browser">在浏览器打开</string>
    <string name="action_share">分享</string>
    <string name="state_loading">正在加载…</string>
    <string name="state_updated">更新于 %%1$s</string>
    <string name="pull_hint">下拉刷新</string>
    <string name="pull_ready">松开刷新</string>
    <string name="refresh_done">正在刷新…</string>
    <string name="error_title">网络连接失败</string>
</resources>
""")

    w("values/styles.xml", """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="TabLabel">
        <item name="android:layout_width">wrap_content</item>
        <item name="android:layout_height">wrap_content</item>
        <item name="android:layout_marginTop">3dp</item>
        <item name="android:textSize">11sp</item>
        <item name="android:textColor">@color/tab_inactive</item>
        <item name="android:singleLine">true</item>
    </style>

    <style name="TabIcon">
        <item name="android:layout_width">24dp</item>
        <item name="android:layout_height">24dp</item>
        <item name="android:tint">@color/tab_inactive</item>
    </style>
</resources>
""")

    w("values/themes.xml", """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- 不依赖 AppCompat / Material，使用框架自带的 Material 主题 -->
    <style name="AppTheme" parent="@android:style/Theme.Material.Light.NoActionBar">
        <item name="android:colorPrimary">@color/brand</item>
        <item name="android:colorPrimaryDark">@color/brand_dark</item>
        <item name="android:colorAccent">@color/accent</item>
        <item name="android:statusBarColor">@color/brand</item>
        <item name="android:windowLightStatusBar">false</item>
        <item name="android:navigationBarColor">@color/tab_bar_bg</item>
        <item name="android:windowLightNavigationBar">true</item>
        <item name="android:windowBackground">@color/paper</item>
    </style>
</resources>
""")

    print("== drawable ==")
    # 自适应图标前景（108 视口，内容压在 66dp 安全区内）
    w("drawable/ic_launcher_foreground.xml", """<?xml version="1.0" encoding="utf-8"?>
<!-- 与 tools/gen_resources_v110.py 的 PNG 同源：坐标 = 512 设计稿 / 4.74 -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <!-- 金色圆环 -->
    <path
        android:pathData="M22,54 A32,32 0 1,0 86,54 A32,32 0 1,0 22,54"
        android:strokeColor="%(gold)s"
        android:strokeWidth="4.2"
        android:fillColor="#00000000" />
    <!-- 三根上行柱（圆头粗线） -->
    <path android:pathData="M44,62 L44,52" android:strokeColor="%(white)s"
        android:strokeWidth="7" android:strokeLineCap="round" />
    <path android:pathData="M54,62 L54,44" android:strokeColor="%(white)s"
        android:strokeWidth="7" android:strokeLineCap="round" />
    <path android:pathData="M64,62 L64,36" android:strokeColor="%(white)s"
        android:strokeWidth="7" android:strokeLineCap="round" />
    <!-- 金色圆点 -->
    <path android:pathData="M64,28 m-3.2,0 a3.2,3.2 0 1,0 6.4,0 a3.2,3.2 0 1,0 -6.4,0"
        android:fillColor="%(gold)s" />
</vector>
""" % dict(gold=GOLD, white=WHITE))

    # 标题栏小 logo（同款标记，24dp，白色 + 金色，随 tint 失效处保持本色）
    w("drawable/ic_logo.xml", """<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="24dp"
    android:height="24dp"
    android:viewportWidth="24"
    android:viewportHeight="24">
    <path android:pathData="M2,12 A10,10 0 1,0 22,12 A10,10 0 1,0 2,12"
        android:strokeColor="#FFFFFF" android:strokeWidth="1.6"
        android:fillColor="#00000000" android:strokeAlpha="0.85" />
    <path android:pathData="M8,16 L8,12.5" android:strokeColor="#FFFFFF"
        android:strokeWidth="2.2" android:strokeLineCap="round" />
    <path android:pathData="M12,16 L12,10" android:strokeColor="#FFFFFF"
        android:strokeWidth="2.2" android:strokeLineCap="round" />
    <path android:pathData="M16,16 L16,8" android:strokeColor="#FFFFFF"
        android:strokeWidth="2.2" android:strokeLineCap="round" />
    <path android:pathData="M16,5.6 m-1.4,0 a1.4,1.4 0 1,0 2.8,0 a1.4,1.4 0 1,0 -2.8,0"
        android:fillColor="#%(gold)s" />
</vector>
""" % dict(gold=GOLD))

    # 在浏览器打开（地球）
    w("drawable/ic_browser.xml", """<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="24dp"
    android:height="24dp"
    android:viewportWidth="24"
    android:viewportHeight="24"
    android:tint="@color/on_brand">
    <path android:fillColor="#FFFFFF"
        android:pathData="M12,2a10,10 0 1,0 0,20a10,10 0 1,0 0,-20zM12,4c1.4,0 2.8,1.4 3.6,4H8.4C9.2,5.4 10.6,4 12,4zM7.9,10h8.2c0.1,0.7 0.1,1.3 0.1,2s0,1.3 -0.1,2H7.9C7.8,13.3 7.8,12.7 7.8,12s0,-1.3 0.1,-2zM4.3,10h2.2c0,0.7 -0.1,1.3 -0.1,2s0,1.3 0.1,2H4.3C4.1,13.4 4,12.7 4,12s0.1,-1.4 0.3,-2zM8.4,16h7.2C14.8,18.6 13.4,20 12,20s-2.8,-1.4 -3.6,-4zM17.5,10h2.2c0.2,0.6 0.3,1.3 0.3,2s-0.1,1.4 -0.3,2h-2.2c0,-0.7 0.1,-1.3 0.1,-2s0,-1.3 -0.1,-2z" />
</vector>
""")

    print("== 自适应图标 ==")
    for name in ("ic_launcher", "ic_launcher_round"):
        w("mipmap-anydpi-v26/%s.xml" % name, """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background" />
    <foreground android:drawable="@drawable/ic_launcher_foreground" />
</adaptive-icon>
""")

    print("== PNG 图标 ==")
    for density, px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
                        ("xxhdpi", 144), ("xxxhdpi", 192)):
        out_dir = os.path.join(RES, "mipmap-%s" % density)
        os.makedirs(out_dir, exist_ok=True)
        draw_icon(px, round_bg=False).save(
            os.path.join(out_dir, "ic_launcher.png"), "PNG", optimize=True)
        draw_icon(px, round_bg=True).save(
            os.path.join(out_dir, "ic_launcher_round.png"), "PNG", optimize=True)
        print("  mipmap-%-8s %3d px" % (density + ":", px))

    # 512 版给 README / Release 页用（仓库外不放，这里顺带产出到 build 目录外的 res 同级）
    draw_icon(512).save(os.path.join(REPO, "android", "icon-512-v110.png"), "PNG", optimize=True)
    print("  android/icon-512-v110.png 512 px")

    print("\n完成。")


if __name__ == "__main__":
    main()
