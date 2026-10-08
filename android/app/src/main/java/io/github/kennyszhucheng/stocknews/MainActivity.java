package io.github.kennyszhucheng.stocknews;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
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

/**
 * 单 Activity + WebView 外壳：把已发布的网页版（GitHub Pages）打包成手机小应用。
 *
 * 功能对照：
 *  1. 首页            -> URL_DAILY（索引页，含“今日速览”）
 *  2. 底部三个入口    -> tabDaily / tabWeb / tabReview，见 {@link #applyTabHighlight(int)}
 *  3. 顶部标题栏      -> titleBar + btnRefresh，进度条 progressBar
 *  4. 返回键          -> {@link #handleBack()}
 *  5. 错误页          -> assets/error.html + {@link Bridge#retry()}
 *  6. 外链跳浏览器    -> {@link #handleUri(Uri)}
 *  7. WebView 配置    -> {@link #configureWebView()}
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
    /** 2. 复盘看板。 */
    private static final String URL_REVIEW = SITE_PREFIX + "review-latest.html";
    /** 5. 本地错误页（离线可用）。 */
    private static final String URL_ERROR = "file:///android_asset/error.html";

    private static final int TAB_DAILY = 0;
    private static final int TAB_WEB = 1;
    private static final int TAB_REVIEW = 2;
    private static final int TAB_COUNT = 3;

    /** 回到前台超过该时长则自动重新加载，保证“每天自动更新”。 */
    private static final long STALE_RELOAD_MS = 30L * 60L * 1000L;

    /* ===================== 视图 ===================== */

    private WebView webView;
    private ProgressBar progressBar;
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

    /* ===================== 生命周期 ===================== */

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        bindViews();
        applyWindowInsets();
        configureWebView();

        if (savedInstanceState != null) {
            // 重建（如旋转 / 进程恢复）：还原 WebView 历史
            webView.restoreState(savedInstanceState);
        } else {
            openUrl(URL_DAILY);
        }
        applyTabHighlight(TAB_DAILY);
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

        tabViews[TAB_DAILY] = findViewById(R.id.tabDaily);
        tabViews[TAB_WEB] = findViewById(R.id.tabWeb);
        tabViews[TAB_REVIEW] = findViewById(R.id.tabReview);

        tabIcons[TAB_DAILY] = findViewById(R.id.iconDaily);
        tabIcons[TAB_WEB] = findViewById(R.id.iconWeb);
        tabIcons[TAB_REVIEW] = findViewById(R.id.iconReview);

        tabLabels[TAB_DAILY] = findViewById(R.id.labelDaily);
        tabLabels[TAB_WEB] = findViewById(R.id.labelWeb);
        tabLabels[TAB_REVIEW] = findViewById(R.id.labelReview);

        // 2. 底部三个入口
        tabViews[TAB_DAILY].setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { onTabSelected(TAB_DAILY, URL_DAILY); }
        });
        tabViews[TAB_WEB].setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { onTabSelected(TAB_WEB, URL_WEB); }
        });
        tabViews[TAB_REVIEW].setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { onTabSelected(TAB_REVIEW, URL_REVIEW); }
        });

        // 3. 刷新按钮
        ImageButton refresh = findViewById(R.id.btnRefresh);
        refresh.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { refresh(); }
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

    /* ===================== 7. WebView 配置 ===================== */

    @SuppressWarnings("deprecation")
    private void configureWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);                 // 7. JS
        s.setDomStorageEnabled(true);                 // 7. DOM storage
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);                   // 7. 宽视口
        s.setLoadWithOverviewMode(true);              // 7. 概览模式
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);     // 7. 缓存开启
        s.setAllowFileAccess(true);                   // 本地错误页 file:///android_asset
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
                return handleUri(request.getUrl());   // 6. 外链判定
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
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (progressBar != null) {
                    progressBar.setProgress(100);
                    progressBar.setVisibility(View.GONE);
                }
                if (URL_ERROR.equals(url) || showingErrorPage) {
                    return;
                }
                lastLoadedAt = System.currentTimeMillis();
                lastRequestedUrl = url;
                applyTabHighlight(tabIndexForUrl(url));   // 2. 当前项高亮
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

    /* ===================== 2. 底部导航 ===================== */

    private void onTabSelected(int index, String url) {
        applyTabHighlight(index);
        if (showingErrorPage) {
            openUrl(url);
            return;
        }
        String current = webView.getUrl();
        if (current != null && tabIndexForUrl(current) == index
                && current.startsWith(SITE_PREFIX)) {
            webView.reload();     // 再点当前项 = 刷新
        } else {
            openUrl(url);
        }
    }

    /** 当前项高亮：选中项用强调色，其余灰色。 */
    private void applyTabHighlight(int active) {
        if (active < 0 || active >= TAB_COUNT) {
            active = TAB_DAILY;
        }
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

    /* ===================== 加载 / 刷新 / 错误页 ===================== */

    private void openUrl(String url) {
        showingErrorPage = false;
        lastRequestedUrl = url;
        webView.loadUrl(url);
    }

    private void refresh() {
        if (showingErrorPage) {
            openUrl(lastRequestedUrl != null ? lastRequestedUrl : URL_DAILY);
        } else {
            webView.reload();
        }
    }

    /** 5. 显示本地错误页，并记住失败地址以便重试。 */
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
        webView.loadUrl(URL_ERROR);
    }

    /* ===================== 6. 外链处理 ===================== */

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

    /* ===================== 4. 返回键 ===================== */

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

    /* ===================== 5. 错误页 JS 桥 ===================== */

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
    }
}
