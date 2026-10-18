package io.github.kennyszhucheng.stocknews;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * 本机 AI 分析页（v1.1.0 新增）。
 *
 * 为什么要有它：网页版是**纯静态**站点，浏览器里既不能保存 API Key（会公开、也会被 CORS
 * 拦住），也不该让站点替你花钱，所以「网页版 AI 分析」不可用是设计上的必然。手机 App 不同：
 * Key 存在**应用私有目录**（SharedPreferences，别的应用读不到），请求由 App 直连
 * api.deepseek.com —— 于是「本机填 Key、本机分析」是可行且合理的。
 *
 * 数据流：拉线上索引页（含「今日速览」）→ 去标签取正文 → 作为上下文交给 DeepSeek →
 * 在页面上展示回答。也支持直接追问（共用同一份日报上下文，省 token）。
 */
public class AiActivity extends Activity {

    private static final String PREFS = "stocknews_ai";
    private static final String KEY_API = "api_key";
    private static final String KEY_MODEL = "model";
    private static final String DEFAULT_MODEL = "deepseek-chat";
    private static final String API_URL = "https://api.deepseek.com/chat/completions";
    /** 线上索引页（含今日速览）；与 MainActivity 的 URL_DAILY 同一地址。 */
    private static final String PAGE_URL = "https://kennyszhucheng.github.io/stock-news-daily/";
    /** 上下文上限：约 8k 字符，够放速览 + 主要板块，又不会烧太多 token。 */
    private static final int MAX_CONTEXT_CHARS = 8000;

    private static final String SYSTEM_PROMPT =
            "你是一名 A 股资讯助手。请仅依据用户提供的日报文本作答：中文、简洁、分点；"
                    + "不给买卖建议、不预测点位涨跌；信息不足就直说“日报里没有提到”。";

    private TextView status;
    private TextView output;
    private EditText input;
    private Button btnAnalyze;
    private Button btnSend;
    private TextView btnKey;

    private String apiKey = "";
    private String model = DEFAULT_MODEL;
    /** 最近一次取到的日报正文（追问复用，避免每次都重新下载）。 */
    private String dailyContext = "";
    private boolean busy = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_ai);

        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        apiKey = sp.getString(KEY_API, "");
        model = sp.getString(KEY_MODEL, DEFAULT_MODEL);

        status = findViewById(R.id.aiStatus);
        output = findViewById(R.id.aiOutput);
        input = findViewById(R.id.aiInput);
        btnAnalyze = findViewById(R.id.btnAnalyze);
        btnSend = findViewById(R.id.btnSend);
        btnKey = findViewById(R.id.btnKey);

        findViewById(R.id.btnBack).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        btnKey.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { showKeyDialog(); }
        });
        btnAnalyze.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { analyzeToday(); }
        });
        btnSend.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { ask(); }
        });

        refreshKeyStatus();
        if (apiKey.isEmpty()) {
            status.setText(R.string.ai_need_key);
        }
    }

    private void refreshKeyStatus() {
        btnKey.setText(apiKey.isEmpty() ? getString(R.string.ai_set_key)
                : getString(R.string.ai_key_saved, tail(apiKey)));
    }

    private static String tail(String key) {
        return key.length() <= 4 ? "****" : key.substring(key.length() - 4);
    }

    /* ===================== 设置 Key / 模型 ===================== */

    private void showKeyDialog() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad / 2, pad, 0);

        final EditText keyInput = new EditText(this);
        keyInput.setHint("sk-...");
        keyInput.setSingleLine(true);
        keyInput.setInputType(InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        keyInput.setText(apiKey);
        box.addView(keyInput);

        final EditText modelInput = new EditText(this);
        modelInput.setHint("模型（默认 " + DEFAULT_MODEL + "）");
        modelInput.setSingleLine(true);
        modelInput.setText(model);
        box.addView(modelInput);

        TextView tip = new TextView(this);
        tip.setText("Key 仅保存在本机应用私有目录，不会上传到任何服务器（除了你自己填的 DeepSeek）。");
        tip.setTextSize(12f);
        tip.setPadding(0, pad / 2, 0, 0);
        box.addView(tip);

        new AlertDialog.Builder(this)
                .setTitle(R.string.ai_set_key)
                .setView(box)
                .setPositiveButton("保存", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int which) {
                        apiKey = keyInput.getText().toString().trim();
                        String m = modelInput.getText().toString().trim();
                        model = m.isEmpty() ? DEFAULT_MODEL : m;
                        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                                .putString(KEY_API, apiKey)
                                .putString(KEY_MODEL, model)
                                .apply();
                        refreshKeyStatus();
                        status.setText(apiKey.isEmpty() ? R.string.ai_need_key
                                : R.string.ai_hint);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /* ===================== 分析 / 追问 ===================== */

    private void analyzeToday() {
        if (!ensureKey()) {
            return;
        }
        setBusy(true, getString(R.string.ai_loading));
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    String page = httpGet(PAGE_URL);
                    final String text = stripHtml(page);
                    dailyContext = text.length() > MAX_CONTEXT_CHARS
                            ? text.substring(0, MAX_CONTEXT_CHARS) : text;
                    final String answer = chat("今天是 "
                            + new SimpleDateFormat("M 月 d 日", Locale.getDefault())
                                    .format(new Date())
                            + "。请基于下面这份股市情报日报，提炼："
                            + "①市场情绪结论 ②值得关注的板块与个股（连同理由）"
                            + "③主要风险提示。最后用一句话说明这份日报的局限。\n\n"
                            + "【日报文本】\n" + dailyContext);
                    ui(new Runnable() {
                        @Override public void run() {
                            output.setText(answer);
                            scrollBottom();
                            setBusy(false, getString(R.string.state_updated,
                                    new SimpleDateFormat("HH:mm", Locale.getDefault())
                                            .format(new Date())));
                        }
                    });
                } catch (final Exception e) {
                    fail(e);
                }
            }
        }).start();
    }

    private void ask() {
        final String q = input.getText().toString().trim();
        if (q.isEmpty()) {
            return;
        }
        if (!ensureKey()) {
            return;
        }
        if (dailyContext.isEmpty()) {
            Toast.makeText(this, "请先点「分析今天的日报」把上下文取回来",
                    Toast.LENGTH_SHORT).show();
            return;
        }
        input.setText("");
        setBusy(true, getString(R.string.ai_loading));
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    final String answer = chat(q + "\n\n【日报文本】\n" + dailyContext);
                    ui(new Runnable() {
                        @Override public void run() {
                            output.append("\n\n————\n\n❓ " + q + "\n\n" + answer);
                            scrollBottom();
                            setBusy(false, getString(R.string.state_updated,
                                    new SimpleDateFormat("HH:mm", Locale.getDefault())
                                            .format(new Date())));
                        }
                    });
                } catch (final Exception e) {
                    fail(e);
                }
            }
        }).start();
    }

    private boolean ensureKey() {
        if (apiKey.isEmpty()) {
            Toast.makeText(this, R.string.ai_need_key, Toast.LENGTH_SHORT).show();
            showKeyDialog();
            return false;
        }
        return true;
    }

    private void fail(final Exception e) {
        ui(new Runnable() {
            @Override public void run() {
                setBusy(false, getString(R.string.ai_error, brief(e)));
            }
        });
    }

    private static String brief(Exception e) {
        String m = e.getMessage();
        if (m == null || m.isEmpty()) {
            m = e.getClass().getSimpleName();
        }
        return m.length() > 160 ? m.substring(0, 160) : m;
    }

    private void setBusy(final boolean b, final String text) {
        busy = b;
        ui(new Runnable() {
            @Override public void run() {
                if (text != null) {
                    status.setText(text);
                }
                btnAnalyze.setEnabled(!b);
                btnSend.setEnabled(!b);
                input.setEnabled(!b);
            }
        });
    }

    private void ui(Runnable r) {
        runOnUiThread(r);
    }

    private void scrollBottom() {
        final ScrollView sv = findViewById(R.id.aiScroll);
        sv.post(new Runnable() {
            @Override public void run() { sv.fullScroll(View.FOCUS_DOWN); }
        });
    }

    /* ===================== 网络 ===================== */

    /** 调用 DeepSeek chat/completions，返回正文。 */
    private String chat(String userText) throws Exception {
        JSONArray messages = new JSONArray();
        messages.put(new JSONObject().put("role", "system").put("content", SYSTEM_PROMPT));
        messages.put(new JSONObject().put("role", "user").put("content", userText));
        JSONObject body = new JSONObject()
                .put("model", model)
                .put("messages", messages)
                .put("temperature", 0.3)
                .put("max_tokens", 1200);

        HttpURLConnection c = (HttpURLConnection) new URL(API_URL).openConnection();
        c.setConnectTimeout(20000);
        c.setReadTimeout(120000);
        c.setRequestMethod("POST");
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        c.setRequestProperty("Authorization", "Bearer " + apiKey);
        c.setDoOutput(true);
        try (OutputStream os = c.getOutputStream()) {
            os.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        String payload = readAll(code >= 400 ? c.getErrorStream() : c.getInputStream());
        if (code != 200) {
            throw new IllegalStateException("HTTP " + code + " " + trim(payload, 200));
        }
        JSONObject o = new JSONObject(payload);
        JSONArray choices = o.optJSONArray("choices");
        if (choices == null || choices.length() == 0) {
            throw new IllegalStateException("响应里没有 choices");
        }
        String content = choices.getJSONObject(0).getJSONObject("message")
                .optString("content", "");
        if (content.isEmpty()) {
            throw new IllegalStateException("模型返回空内容");
        }
        return content.trim();
    }

    private static String httpGet(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(20000);
        c.setReadTimeout(30000);
        c.setRequestProperty("User-Agent", "stocknews-app");
        c.setRequestProperty("Accept", "text/html");
        int code = c.getResponseCode();
        if (code != 200) {
            throw new IllegalStateException("HTTP " + code);
        }
        return readAll(c.getInputStream());
    }

    private static String readAll(InputStream in) throws Exception {
        if (in == null) {
            return "";
        }
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = in.read(b)) > 0) {
            buf.write(b, 0, n);
        }
        return new String(buf.toByteArray(), StandardCharsets.UTF_8);
    }

    private static String trim(String s, int max) {
        if (s == null) {
            return "";
        }
        s = s.replace('\n', ' ').trim();
        return s.length() > max ? s.substring(0, max) : s;
    }

    /** HTML → 纯文本：去掉脚本/样式、标签、常见实体，压掉多余空行。 */
    static String stripHtml(String html) {
        if (html == null) {
            return "";
        }
        String s = html;
        s = Pattern.compile("(?is)<script.*?</script>").matcher(s).replaceAll(" ");
        s = Pattern.compile("(?is)<style.*?</style>").matcher(s).replaceAll(" ");
        s = Pattern.compile("(?is)<head.*?</head>").matcher(s).replaceAll(" ");
        s = Pattern.compile("(?s)<[^>]+>").matcher(s).replaceAll(" ");
        s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", "\"").replace("&#39;", "'");
        s = Pattern.compile("[ \\t\\x0B\\f\\r]+").matcher(s).replaceAll(" ");
        s = Pattern.compile("\\n\\s*\\n+").matcher(s).replaceAll("\n");
        return s.trim();
    }
}
