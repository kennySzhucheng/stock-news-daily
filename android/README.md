# 股市情报 · Android 外壳工程

把已发布的网页版（GitHub Pages）打包成安卓 App。**单 Activity + WebView**，全部用 Java 编写，
不引入任何第三方 / AndroidX 运行时依赖（`app/build.gradle` 的 `dependencies` 故意为空），
因此可以完全离线构建。

- **包名 / applicationId**：`io.github.kennyszhucheng.stocknews`
- **App 名**：股市情报
- **versionName / versionCode**：`1.0.0` / `1`
- **minSdk / targetSdk / compileSdk**：`26` / `35` / `35`
- **AGP / Gradle**：`8.7.3` / `8.9`

---

## 1. 工程结构

```
android/
├─ settings.gradle              # 仓库源（google / mavenCentral）+ include ':app'
├─ build.gradle                 # 声明 AGP 8.7.3（apply false）
├─ gradle.properties            # JVM 内存、AndroidX 开关（无 AndroidX 依赖，仅为消除 AGP 告警）
├─ local.properties             # sdk.dir —— 本机路径，已 gitignore，不入库
├─ tools/
│  └─ make_icons.py             # 从 modules/m9_web/web/icon-512.png 生成 mipmap 图标
└─ app/
   ├─ build.gradle              # 应用模块配置
   ├─ proguard-rules.pro        # 占位（release 未开混淆）
   └─ src/main/
      ├─ AndroidManifest.xml                       # INTERNET 权限、单 Activity
      ├─ assets/error.html                         # 断网时的本地错误页（含重试按钮）
      ├─ java/io/github/kennyszhucheng/stocknews/
      │  └─ MainActivity.java                      # 全部逻辑
      └─ res/
         ├─ layout/activity_main.xml               # 标题栏 + 进度条 + WebView + 底部三入口
         ├─ values/{strings,colors,themes,styles}.xml
         ├─ drawable/ic_refresh.xml                 # 刷新按钮图标
         ├─ drawable/ic_tab_{daily,web,review}.xml  # 底部三个入口图标（VectorDrawable）
         └─ mipmap-{m,h,xh,xxh,xxxh}dpi/ic_launcher[_round].png  # 由 icon-512.png 生成
```

## 2. 功能 → 代码位置

| # | 功能 | 位置 |
|---|------|------|
| 1 | 首页 = 索引页（含「今日速览」） | `MainActivity.java` 常量 `URL_DAILY`；`onCreate` 里 `openUrl(URL_DAILY)` |
| 2 | 底部三个入口（日报 / 交互版 / 复盘看板） | `res/layout/activity_main.xml` 的 `tabDaily / tabWeb / tabReview`；`MainActivity#bindViews`、`#onTabSelected` |
| 2 | 当前项高亮 | `MainActivity#applyTabHighlight`（选中 `@color/accent`，未选中 `@color/tab_inactive`）；`#tabIndexForUrl` |
| 3 | 顶部标题栏「股市情报」+ 刷新按钮 | `activity_main.xml` 的 `titleBar` / `titleText` / `btnRefresh`（`@string/app_name`、`@drawable/ic_refresh`）；`MainActivity#refresh` |
| 3 | 加载时水平进度条 | `activity_main.xml` 的 `progressBar`；`WebChromeClient#onProgressChanged`、`WebViewClient#onPageStarted/onPageFinished` |
| 4 | 返回键：可后退则后退，否则退出 | `MainActivity#onKeyDown` / `#onBackPressed` / `#handleBack` |
| 5 | 断网 / 加载失败显示本地错误页 + 重试 | `assets/error.html`；`WebViewClient#onReceivedError`、`#onReceivedHttpError` → `#showErrorPage`；重试走 JS 桥 `MainActivity.Bridge#retry` |
| 6 | host 不是 `kennyszhucheng.github.io` 的链接用系统浏览器打开 | `MainActivity#handleUri` / `#openExternally`（`shouldOverrideUrlLoading`） |
| 7 | WebView：JS / DOM storage / useWideViewPort / loadWithOverviewMode / 缓存开启 | `MainActivity#configureWebView` |
| 8 | 图标由仓库内 `icon-512.png` 生成 | `tools/make_icons.py` → `res/mipmap-*/ic_launcher.png` |
| 9 | 包名 / 版本 / SDK 等级 | `app/build.gradle`、`src/main/AndroidManifest.xml` |

补充行为：

- **错误页上的返回键直接退出应用**（`handleBack` 里 `clearHistory()`），避免反复撞上打不开的地址。
- **再点一次当前底部入口 = 刷新**（`onTabSelected`）。
- **回到前台超过 30 分钟自动重载**（`onResume` + `STALE_RELOAD_MS`），保证每天看到的是当天内容。
- targetSdk 35 下 Android 15 强制 edge-to-edge，`MainActivity#applyWindowInsets` 把系统栏高度补成
  标题栏 / 底部导航的内边距，避免被状态栏和手势条压住。为此 `activity_main.xml` 里
  `titleBar` / `tabBar` 是 **`wrap_content` 的外层**，真正的 56dp / 58dp 设计高度在内层
  `titleRow` / `tabRow` 上 —— 否则在状态栏偏高的机器（如带刘海、状态栏 128px 的 AVD）上，
  padding 会把标题文字和底部标签挤没。

## 3. 本机工具链（本工程**不使用** Gradle wrapper）

| 工具 | 路径 |
|------|------|
| Gradle 8.9（已缓存） | `C:\Users\Monesy\.gradle\wrapper\dists\gradle-8.9-bin\90cnw93cvbtalezasaz0blq0a\gradle-8.9\bin\gradle.bat` |
| JDK 21 | `D:\CTF\java`（`java` 走 `C:\Program Files\Common Files\Oracle\Java\javapath` 这个 shim；真实根目录从注册表 `HKLM\SOFTWARE\JavaSoft\JDK\21.0.4` 的 `JavaHome` 读出来） |
| Android SDK | `C:\Users\Monesy\AppData\Local\Android\Sdk`（platform `android-35`、build-tools `35.0.0`） |

> 仓库里**没有** `gradlew` / `gradle/wrapper/`，这是刻意为之：本机 Gradle 8.9 已经解压在
> `~/.gradle/wrapper/dists/` 下，直接调用那个 `gradle.bat` 即可，不必再生成 wrapper。

## 4. 重新构建

在 `android/` 目录下：

```powershell
$env:JAVA_HOME = 'D:\CTF\java'
$env:ANDROID_HOME = 'C:\Users\Monesy\AppData\Local\Android\Sdk'

& 'C:\Users\Monesy\.gradle\wrapper\dists\gradle-8.9-bin\90cnw93cvbtalezasaz0blq0a\gradle-8.9\bin\gradle.bat' assembleDebug --console=plain
```

产物：`android/app/build/outputs/apk/debug/app-debug.apk`

> `JAVA_HOME` 没设时 AGP 会报找不到 JDK，必须先设。若 `local.properties` 丢失，
> 重新写一行 `sdk.dir=C\:\\Users\\Monesy\\AppData\\Local\\Android\\Sdk`（该文件已 gitignore）。
>
> 首次构建需要联网从 google() / mavenCentral() 拉 AGP 8.7.3 及其传递依赖。
> 本机是**直连**，不要配代理；如果环境里残留 `HTTP_PROXY` / `HTTPS_PROXY`，先清掉再跑。

### 重新生成图标

```powershell
python android\tools\make_icons.py
```

## 5. 改 URL

要改站点地址，只动 `MainActivity.java` 顶部这几个常量（`SITE_PREFIX` 会同时影响
`URL_DAILY` / `URL_WEB` / `URL_REVIEW`）：

```java
private static final String SITE_HOST = "kennyszhucheng.github.io";
private static final String SITE_PREFIX = "https://" + SITE_HOST + "/stock-news-daily/";

private static final String URL_DAILY  = SITE_PREFIX;                          // 首页
private static final String URL_WEB    = SITE_PREFIX + "web/";                 // 交互版
private static final String URL_REVIEW = SITE_PREFIX + "review-latest.html";   // 复盘看板
```

**注意**：`SITE_HOST` 同时也是「站内 / 站外」的判定依据 —— 只有该 host（及其子域）的链接
留在应用内，其它链接一律丢给系统浏览器。换域名时这两个地方要一起改。

`error.html` 里的兜底跳转地址（`location.href = ...`）也写着同一个首页 URL，换域名时一并改。

改完重新跑第 4 节的 `assembleDebug`。

## 6. 装到手机

### 6.1 模拟器

```powershell
& "$env:ANDROID_HOME\emulator\emulator.exe" -avd test35 -no-window -no-audio -no-snapshot-save
adb wait-for-device
adb install -r android\app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n io.github.kennyszhucheng.stocknews/.MainActivity
```

### 6.2 真机（USB）

1. 手机开「开发者选项」→ 打开「USB 调试」；
2. 数据线连电脑，手机弹出「允许 USB 调试吗？」→ 勾选「一律允许」→ 确定；
3. `adb devices` 能看到设备序列号即可；
4. 安装：

```powershell
adb install -r <apk 路径>
```

> ⚠️ **同时连着模拟器和真机时，`adb install` 会报 `more than one device/emulator`。**
> 先用 `adb devices` 看序列号，然后每条命令都加 `-s`，例如
> `adb -s emulator-5554 install -r <apk>` 或 `adb -s 10AG1P18NB003DP install -r <apk>`。
> 别在没加 `-s` 的情况下跑安装命令，否则可能装到不想装的设备上。

`-r` 表示覆盖安装（保留数据）。如果报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，说明机器上
已有的同名应用签名不同，先 `adb uninstall io.github.kennyszhucheng.stocknews` 再装。

### 6.3 直接拷 APK 到手机

把 APK 传到手机（微信文件传输 / 数据线 / 网盘），用手机的文件管理器点开安装。

### 6.4 未知来源应用

Android 8.0 及以上是**按来源应用**授权的，不是全局开关：

- 系统设置 → 应用 → 特殊应用权限 → **安装未知应用**；
- 找到你用来打开 APK 的那个应用（文件管理器 / 浏览器 / 微信 / QQ），打开「允许来自此来源的应用」；
- 部分国产 ROM 路径不同（如「设置 → 安全 → 更多安全设置 → 安装外部来源应用」），
  按提示走即可；
- 装完可以再把这个开关关掉，属正常做法。

首次打开 App 时系统会再次弹窗确认，选「仍要安装 / 允许」即可。

## 7. Release 签名与升级

**同一个 keystore 才能覆盖升级**（Android 只认签名，签名不一致会拒绝安装，包名相同也不行）。

### 7.1 签名材料在哪

| 用途 | 位置 |
|------|------|
| keystore | `C:\Users\Monesy\.stock-news-daily\stock-news-daily-release.jks` |
| **口令** | 存在 `~/.stock-news-daily/android-keystore.txt` |
| 已签名 release APK | `C:\Users\Monesy\.stock-news-daily\股市情报助手-v1.0.0.apk` |

`~/.stock-news-daily/` 在仓库之外，**绝不入库**；根 `.gitignore` 另外还挡掉了 `*.keystore` / `*.jks`。
本 README 按约定不写任何明文口令。

查看口令（含 alias、storepass、keypass）：

```powershell
Get-Content "$env:USERPROFILE\.stock-news-daily\android-keystore.txt"
```

> ⚠️ **务必自己再备份一份这个 keystore 文件和口令**（比如放到网盘 / 密码管理器）。
> 一旦丢失，就无法再发布能覆盖升级的新版本，只能换包名重来。

### 7.2 构建 release 并签名

```powershell
$env:JAVA_HOME = 'D:\CTF\java'
$env:ANDROID_HOME = 'C:\Users\Monesy\AppData\Local\Android\Sdk'
$BT = "$env:ANDROID_HOME\build-tools\35.0.0"
$GRADLE = 'C:\Users\Monesy\.gradle\wrapper\dists\gradle-8.9-bin\90cnw93cvbtalezasaz0blq0a\gradle-8.9\bin\gradle.bat'
$OUT = "$env:USERPROFILE\.stock-news-daily"

# 1) 产出未签名 release APK
& $GRADLE assembleRelease --console=plain

# 2) zipalign
& "$BT\zipalign.exe" -p -f 4 `
   'app\build\outputs\apk\release\app-release-unsigned.apk' `
   "$OUT\股市情报助手-v1.0.0-aligned.apk"

# 3) 从口令文件里读出 alias / 口令，交给 apksigner 签名
$read  = Get-Content "$OUT\android-keystore.txt"
$alias = ($read | Where-Object { $_ -match '^alias=' })     -replace '^alias=',''
$sp    = ($read | Where-Object { $_ -match '^storepass=' }) -replace '^storepass=',''
$kp    = ($read | Where-Object { $_ -match '^keypass=' })   -replace '^keypass=',''

& "$BT\apksigner.bat" sign `
   --ks "$OUT\stock-news-daily-release.jks" `
   --ks-key-alias $alias `
   --ks-pass "pass:$sp" `
   --key-pass "pass:$kp" `
   --out "$OUT\股市情报助手-v1.0.0.apk" `
   "$OUT\股市情报助手-v1.0.0-aligned.apk"

# 4) 校验
& "$BT\apksigner.bat" verify --print-certs "$OUT\股市情报助手-v1.0.0.apk"
```

签名结果应为 **v2 + v3 通过、v1 (JAR signing) 为 false** —— 因为 `minSdk 26`（≥ 24），
Android 8.0 以上全部支持 v2/v3，apksigner 不会再生成 v1 签名，这是预期行为。

> release 构建**没有**在 `app/build.gradle` 里配 `signingConfig`，所以 Gradle 产出的是
> `app-release-unsigned.apk`，签名完全由 build-tools 里的 `apksigner` 完成。
> 这样口令不会出现在任何入库文件里。

### 7.3 后续版本升级（覆盖安装）

1. 改 `app/build.gradle` 里的 `versionCode`（必须**严格递增**）和 `versionName`；
2. **仍然用上面那个 keystore 和同一把 alias**，重复 7.2 的签名流程；
3. 用户手机上直接 `adb install -r <新 apk>` 或用文件管理器点开覆盖安装即可升级，
   数据保留。

### 7.4 重新生成 keystore（只在第一次或确定要换签名时）

```powershell
& 'D:\CTF\java\bin\keytool.exe' -genkeypair -v `
  -keystore "$env:USERPROFILE\.stock-news-daily\stock-news-daily-release.jks" `
  -alias stocknews -keyalg RSA -keysize 2048 -validity 10950
```

换 keystore = 换签名 = 老用户**无法**覆盖升级，只能卸载重装，请谨慎。

## 8. 已知限制

- 应用只是网页外壳：**断网时除了那张本地错误页，没有离线内容**。
- 网页版的分享 / 深链（如果以后加了）不会唤起本 App —— 没有配 `intent-filter` 的
  `VIEW` / App Links。
- 没有自定义 `WebViewClient` 的 SSL 错误处理，证书异常会走错误页。
- release 未开启混淆与资源压缩（`minifyEnabled false` / `shrinkResources false`），
  包体略大；本工程无第三方依赖，开启与否差别很小。
