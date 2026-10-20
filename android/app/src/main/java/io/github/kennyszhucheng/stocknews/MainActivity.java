package io.github.kennyszhucheng.stocknews;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.ColorStateList;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 单 Activity + WebView 外壳：把已发布的网页版（GitHub Pages）打包成手机小应用。
 *
 * v1.1.0（2026-10-19）的“人性化”改动：
 *  1. 顶部栏        -> 小 logo + 「更新于 HH:mm」副标题（一眼知道内容新不新）
 *  2. 在浏览器打开  -> 顶部 globe 按钮 / 错误页按钮（长按分享、复制链接都靠它）
 *  3. 下拉刷新      -> 页面在顶部时向下拖 72dp 松手即刷新（不用去够右上角按钮）
 *  4. 记忆上次 tab  -> 下次打开直接回到上次看的入口（SharedPreferences）
 *  5. 首屏加载遮罩  -> 打开时不再是一屏空白，而是「正在加载…」
 *  6. 更精致的图标  -> 自适应图标（金环 + 三根上行柱）+ 重新绘制的 PNG
 *  既有功能不变：底部三入口、返回键、外链跳浏览器、断网错误页 + 重试。
 */
public class MainActivity extends Activity {

    /* ===================== 站点常量 ===================== */

    /** 站内 host：只有该 host（及其子域）在应用内打开，其余交给系统浏览器。 */
    private static final String SITE_HOST = "kennyszhucheng.github.io";
    private static final String SITE_PREFIX = "https://" + SITE_HOST + "/stock-news-daily/";

    /** 1. 首页（索引页，含“今日速览”）。 */
    private static final String URL_DAILY = SITE_PREFIX;
    /** 2. 交互版。 */
    private static final String URL_WEB = SITE_PREFIX + "web/";
    /** 3. 复盘看板。 */
    private static final String URL_REVIEW = SITE_PREFIX + "review-latest.html";
    /** 本地错误页（离线可用）。 */
    private static final String URL_ERROR = "file:///android_asset/error.html";

    private static final int TAB_DAILY = 0;
    private static final int TAB_WEB = 1;
    private static final int TAB_REVIEW = 2;
    private static final int TAB_COUNT = 3;

    private static final String[] TAB_URLS = {URL_DAILY, URL_WEB, URL_REVIEW};

    /** 回到前台超过该时长则自动重新加载，保证“每天自动更新”。 */
    private static final long STALE_RELOAD_MS = 30L * 60L * 1000L;
    /** 下拉刷新触发距离（dp）。 */
    private static final int PULL_THRESHOLD_DP = 72;

    private static final String PREFS = "stocknews";
    private static final String KEY_TAB = "last_tab";

    /* ===================== 视图 ===================== */

    private WebView webView;
    private ProgressBar progressBar;
    private TextView subtitleText;
    private TextView pullHint;
    private View loadingOverlay;
    private final View[] tabViews = new View[TAB_COUNT];
    private final ImageView[] tabIcons = new ImageView[TAB_COUNT];
    private final TextView[] tabLabels = new TextView[TAB_COUNT];

    /* ===================== 状态 ===================== */

    /** 最近一次真正请求的线上地址，错误页的“重新加载”会回到它。 */
    private String lastRequestedUrl = URL_DAILY;
    /** 当前是否正停留在本地错误页。 */
    private boolean showingErrorPage = false;
    /** 最近一次加载完成时间（用于前台恢复时的自动刷新）。 */
    private long lastLoadedAt = 0L;
    /** 当前高亮的入口。 */
    private int currentTab = TAB_DAILY;
    /** 首屏遮罩是否已经撤掉（撤掉后不再显示，避免每次刷新闪白）。 */
    private boolean firstLoadDone = false;

    /** 下拉手势：按下点 y、是否正在跟踪。 */
    private float pullDownY = -1f;
    private boolean pullTracking = false;

    /* ===================== 生命周期 ===================== */

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        bindViews();
        applyWindowInsets();
        configureWebView();
        setupPullToRefresh();
        setupBrowserButton();

        if (savedInstanceState != null) {
            // 重建（如旋转 / 进程恢复）：还原 WebView 历史
            webView.restoreState(savedInstanceState);
            firstLoadDone = true;
            showLoading(false);
        } else {
            // 4. 记忆上次入口
            currentTab = readSavedTab();
            showLoading(true);
            openUrl(TAB_URLS[currentTab]);
        }
        applyTabHighlight(currentTab);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        if (webView != null) {
            webView.saveState(outState);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 长时间挂后台后回到前台：自动刷新，避免看到过期内容
        if (!showingErrorPage && webView != null && lastLoadedAt > 0L
                && System.currentTimeMillis() - lastLoadedAt > STALE_RELOAD_MS) {
            webView.reload();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.setWebChromeClient(null);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    /* ===================== 视图绑定 ===================== */

    private void bindViews() {
        webView = findViewById(R.id.webView);
        progressBar = findViewById(R.id.progressBar);
        subtitleText = findViewById(R.id.subtitleText);
        pullHint = findViewById(R.id.pullHint);
        loadingOverlay = findViewById(R.id.loadingOverlay);

        tabViews[TAB_DAILY] = findViewById(R.id.tabDaily);
        tabViews[TAB_WEB] = findViewById(R.id.tabWeb);
        tabViews[TAB_REVIEW] = findViewById(R.id.tabReview);

        tabIcons[TAB_DAILY] = findViewById(R.id.iconDaily);
        tabIcons[TAB_WEB] = findViewById(R.id.iconWeb);
        tabIcons[TAB_REVIEW] = findViewById(R.id.iconReview);

        tabLabels[TAB_DAILY] = findViewById(R.id.labelDaily);
        tabLabels[TAB_WEB] = findViewById(R.id.labelWeb);
        tabLabels[TAB_REVIEW] = findViewById(R.id.labelReview);

        for (int i = 0; i < TAB_COUNT; i++) {
            final int index = i;
            tabViews[i].setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { onTabSelected(index); }
            });
        }

        ImageButton refresh = findViewById(R.id.btnRefresh);
        refresh.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { refresh(); }
        });

        // v1.1.0：本机 AI 分析入口（本地 Key，直连 DeepSeek，不经过网页版）
        findViewById(R.id.btnAi).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, AiActivity.class));
            }
        });
    }

    /**
     * targetSdk 35 下 Android 15 强制 edge-to-edge，需要自己把系统栏高度
     * 作为 padding 补给标题栏 / 底部导航，否则会被状态栏和手势条压住。
     */
    private void applyWindowInsets() {
        final View root = findViewById(R.id.root);
        final View titleBar = findViewById(R.id.titleBar);
        final View tabBar = findViewById(R.id.tabBar);

        final int titleLeft = titleBar.getPaddingLeft();
        final int titleRight = titleBar.getPaddingRight();
        final int titleTop = titleBar.getPaddingTop();
        final int titleBottom = titleBar.getPaddingBottom();
        final int tabLeft = tabBar.getPaddingLeft();
        final int tabTop = tabBar.getPaddingTop();
        final int tabRight = tabBar.getPaddingRight();
        final int tabBottom = tabBar.getPaddingBottom();

        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override
            public WindowInsets onApplyWindowInsets(View v, WindowInsets insets) {
                int top = insets.getSystemWindowInsetTop();
                int bottom = insets.getSystemWindowInsetBottom();
                titleBar.setPadding(titleLeft, titleTop + top, titleRight, titleBottom);
                tabBar.setPadding(tabLeft, tabTop, tabRight, tabBottom + bottom);
                return insets;
            }
        });
        root.requestApplyInsets();
    }

    /* ===================== WebView 配置 ===================== */

    @SuppressWarnings("deprecation")
    private void configureWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setSupportMultipleWindows(false);
        s.setDefaultTextEncodingName("UTF-8");

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setVerticalScrollBarEnabled(true);
        webView.setHorizontalScrollBarEnabled(false);
        webView.addJavascriptInterface(new Bridge(), "AndroidBridge");

        webView.setWebViewClient(new WebViewClient() {

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleUri(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleUri(Uri.parse(url));
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                if (progressBar != null) {
                    progressBar.setVisibility(View.VISIBLE);
                    progressBar.setProgress(0);
                }
                if (!URL_ERROR.equals(url)) {
                    showingErrorPage = false;
                    lastRequestedUrl = url;
                    setSubtitle(getString(R.string.state_loading));
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (progressBar != null) {
                    progressBar.setProgress(100);
                    progressBar.setVisibility(View.GONE);
                }
                if (URL_ERROR.equals(url) || showingErrorPage) {
                    showLoading(false);
                    return;
                }
                lastLoadedAt = System.currentTimeMillis();
                lastRequestedUrl = url;
                firstLoadDone = true;
                showLoading(false);
                // 1. 更新时间：一眼看出内容是几点拉到的
                setSubtitle(getString(R.string.state_updated,
                        new SimpleDateFormat("HH:mm", Locale.getDefault())
                                .format(new Date(lastLoadedAt))));
                applyTabHighlight(tabIndexForUrl(url));
                // 2026-10-20：把网页版持仓（localStorage）导出到应用外部目录，
                // 便于"手机端持仓同步到电脑"（adb pull）与换机备份
                exportPortfolio();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    showErrorPage(request.getUrl().toString());
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request,
                                            android.webkit.WebResourceResponse errorResponse) {
                if (request != null && request.isForMainFrame()
                        && errorResponse != null && errorResponse.getStatusCode() >= 400) {
                    showErrorPage(request.getUrl().toString());
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (progressBar == null) {
                    return;
                }
                progressBar.setProgress(newProgress);
                if (newProgress >= 100) {
                    progressBar.setVisibility(View.GONE);
                } else if (progressBar.getVisibility() != View.VISIBLE) {
                    progressBar.setVisibility(View.VISIBLE);
                }
            }
        });
    }

    /* ===================== 3. 下拉刷新（不引入 AndroidX） ===================== */

    private void setupPullToRefresh() {
        webView.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, MotionEvent event) {
                switch (event.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        pullDownY = event.getY();
                        pullTracking = webView.getScrollY() == 0;
                        break;
                    case MotionEvent.ACTION_MOVE: {
                        if (!pullTracking || pullDownY < 0) {
                            break;
                        }
                        if (webView.getScrollY() > 0) {
                            pullTracking = false;
                            hidePullHint();
                            break;
                        }
                        float dy = event.getY() - pullDownY;
                        if (dy > dp(20)) {
                            boolean ready = dy > dp(PULL_THRESHOLD_DP);
                            showPullHint(ready
                                    ? getString(R.string.pull_ready)
                                    : getString(R.string.pull_hint));
                        } else {
                            hidePullHint();
                        }
                        break;
                    }
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL: {
                        float dy = pullDownY < 0 ? 0 : event.getY() - pullDownY;
                        boolean trigger = pullTracking && dy > dp(PULL_THRESHOLD_DP);
                        hidePullHint();
                        pullTracking = false;
                        pullDownY = -1f;
                        if (trigger) {
                            Toast.makeText(MainActivity.this,
                                    R.string.refresh_done, Toast.LENGTH_SHORT).show();
                            refresh();
                        }
                        break;
                    }
                    default:
                        break;
                }
                return false;   // 不拦截：滚动仍交给 WebView
            }
        });
    }

    private void showPullHint(String text) {
        if (pullHint == null) {
            return;
        }
        pullHint.setText(text);
        pullHint.setVisibility(View.VISIBLE);
    }

    private void hidePullHint() {
        if (pullHint != null) {
            pullHint.setVisibility(View.GONE);
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    /* ===================== 2. 在浏览器打开 ===================== */

    private void setupBrowserButton() {
        ImageButton browser = findViewById(R.id.btnBrowser);
        browser.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { openCurrentInBrowser(); }
        });
    }

    private void openCurrentInBrowser() {
        String url = showingErrorPage
                ? (lastRequestedUrl != null ? lastRequestedUrl : URL_DAILY)
                : (webView != null && webView.getUrl() != null
                        ? webView.getUrl() : URL_DAILY);
        openExternally(Uri.parse(url));
    }

    /* ===================== 底部导航 ===================== */

    private void onTabSelected(int index) {
        if (index < 0 || index >= TAB_COUNT) {
            return;
        }
        applyTabHighlight(index);
        if (showingErrorPage) {
            openUrl(TAB_URLS[index]);
            return;
        }
        String current = webView.getUrl();
        if (current != null && tabIndexForUrl(current) == index
                && current.startsWith(SITE_PREFIX)) {
            webView.reload();     // 再点当前项 = 刷新
        } else {
            openUrl(TAB_URLS[index]);
        }
    }

    /** 当前项高亮：选中项用强调色，其余灰色；同时记住选择（4）。 */
    private void applyTabHighlight(int active) {
        if (active < 0 || active >= TAB_COUNT) {
            active = TAB_DAILY;
        }
        currentTab = active;
        int on = getColor(R.color.accent);
        int off = getColor(R.color.tab_inactive);
        for (int i = 0; i < TAB_COUNT; i++) {
            ColorStateList csl = ColorStateList.valueOf(i == active ? on : off);
            if (tabIcons[i] != null) {
                tabIcons[i].setImageTintList(csl);
            }
            if (tabLabels[i] != null) {
                tabLabels[i].setTextColor(csl);
            }
        }
        saveTab(active);
    }

    private int tabIndexForUrl(String url) {
        if (url == null) {
            return TAB_DAILY;
        }
        if (url.startsWith(URL_WEB)) {
            return TAB_WEB;
        }
        if (url.startsWith(URL_REVIEW)) {
            return TAB_REVIEW;
        }
        return TAB_DAILY;
    }

    private int readSavedTab() {
        try {
            int i = Integer.parseInt(
                    getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_TAB, "0"));
            return (i >= 0 && i < TAB_COUNT) ? i : TAB_DAILY;
        } catch (Exception e) {
            return TAB_DAILY;
        }
    }

    private void saveTab(int index) {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString(KEY_TAB, String.valueOf(index)).apply();
    }

    /* ===================== 加载 / 刷新 / 错误页 ===================== */

    private void openUrl(String url) {
        showingErrorPage = false;
        lastRequestedUrl = url;
        hidePullHint();
        webView.loadUrl(url);
    }

    private void refresh() {
        if (showingErrorPage) {
            openUrl(lastRequestedUrl != null ? lastRequestedUrl : URL_DAILY);
        } else {
            webView.reload();
        }
    }

    private void showLoading(boolean show) {
        if (loadingOverlay == null) {
            return;
        }
        // 已经加载过一次就不再显示（避免刷新/切 tab 时闪白屏）
        if (show && firstLoadDone) {
            return;
        }
        loadingOverlay.setVisibility(show ? View.VISIBLE : View.GONE);
    }

    private void setSubtitle(String text) {
        if (subtitleText != null && text != null) {
            subtitleText.setText(text);
        }
    }

    /** 显示本地错误页，并记住失败地址以便重试。 */
    private void showErrorPage(String failedUrl) {
        if (showingErrorPage) {
            return;
        }
        showingErrorPage = true;
        if (failedUrl != null && !URL_ERROR.equals(failedUrl)) {
            lastRequestedUrl = failedUrl;
        }
        if (progressBar != null) {
            progressBar.setVisibility(View.GONE);
        }
        showLoading(false);
        hidePullHint();
        webView.loadUrl(URL_ERROR);
    }

    /* ===================== 外链处理 ===================== */

    /** @return true 表示已由本方法处理（应用不加载该 URL） */
    private boolean handleUri(Uri uri) {
        if (uri == null) {
            return false;
        }
        String scheme = uri.getScheme();
        if (scheme == null) {
            return false;
        }
        scheme = scheme.toLowerCase();

        if ("http".equals(scheme) || "https".equals(scheme)) {
            String host = uri.getHost();
            boolean inSite = host != null
                    && (host.equalsIgnoreCase(SITE_HOST)
                        || host.toLowerCase().endsWith("." + SITE_HOST));
            if (inSite) {
                return false;                  // 站内：交给 WebView
            }
            openExternally(uri);               // 站外：系统浏览器
            return true;
        }
        if ("file".equals(scheme) || "about".equals(scheme)
                || "data".equals(scheme) || "javascript".equals(scheme)
                || "blob".equals(scheme)) {
            return false;
        }
        openExternally(uri);                   // mailto: / tel: / intent: ...
        return true;
    }

    private void openExternally(Uri uri) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, uri);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "没有可以打开该链接的应用", Toast.LENGTH_SHORT).show();
        }
    }

    /* ===================== 返回键 ===================== */

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && handleBack()) {
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (!handleBack()) {
            super.onBackPressed();
        }
    }

    /** @return true 表示返回键已被 WebView 吃掉（网页后退） */
    private boolean handleBack() {
        if (webView == null) {
            return false;
        }
        if (showingErrorPage) {
            // 停在错误页时，直接退出，避免反复撞上打不开的地址
            webView.clearHistory();
            return false;
        }
        if (webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return false;
    }

    /* ===================== 持仓导出（v1.1.1） ===================== */

    /**
     * 把 WebView 里「交互版 → 持仓」的 localStorage 落成一个 JSON 文件：
     *   /sdcard/Android/data/<包名>/files/portfolio/portfolio.json
     * 用途：手机端加的持仓同步到电脑（adb pull 后写进本地 data/portfolio.json，
     * 本地网页版打开即自动载入）；也是换手机时的备份。文件在应用外部私有目录，
     * 卸载 App 会一起删掉，不申请任何存储权限。
     */
    private void exportPortfolio() {
        if (webView == null) {
            return;
        }
        webView.evaluateJavascript("localStorage.getItem('snd_portfolio_v1')||''",
                new android.webkit.ValueCallback<String>() {
                    @Override
                    public void onReceiveValue(String value) {
                        try {
                            if (value == null || value.length() < 3) {
                                return;
                            }
                            // evaluateJavascript 回的是 JS 字符串字面量（带引号转义），先还原
                            Object raw = new org.json.JSONTokener(value).nextValue();
                            String json = String.valueOf(raw);
                            if (json.isEmpty() || "null".equals(json)) {
                                return;
                            }
                            java.io.File dir = new java.io.File(
                                    getExternalFilesDir(null), "portfolio");
                            if (!dir.exists() && !dir.mkdirs()) {
                                return;
                            }
                            java.io.FileWriter w = new java.io.FileWriter(
                                    new java.io.File(dir, "portfolio.json"), false);
                            w.write(json);
                            w.close();
                        } catch (Exception ignored) {
                            // 导出是附带能力，失败绝不能影响浏览
                        }
                    }
                });
    }

    /* ===================== 错误页 JS 桥 ===================== */

    /** 必须是 public 的具名类，@JavascriptInterface 才能被 WebView 反射调用。 */
    public class Bridge {
        @JavascriptInterface
        public void retry() {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    openUrl(lastRequestedUrl != null ? lastRequestedUrl : URL_DAILY);
                }
            });
        }

        /** 错误页的「用浏览器打开」：网络正常但 WebView 出问题时仍有路可走。 */
        @JavascriptInterface
        public void openInBrowser() {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    openCurrentInBrowser();
                }
            });
        }
    }
}
