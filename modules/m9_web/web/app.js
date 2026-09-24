/* M9 网页版前端逻辑
 * 同一份代码跑两种数据源：
 *   - 本地服务（server.py）：走 /api/*
 *   - 静态导出（export.py）：走 ./api/*.json，由 window.__STATIC__ 切换
 * 新闻的筛选/排序/分页一律在浏览器端做——数据量小（百级），
 * 这样静态版无需后端，交互也没有请求延迟。
 */
(function () {
  'use strict';

  var STATIC = !!window.__STATIC__;
  var PAGE = 40;

  var CAT_CN = { policy: '宏观政策', stock: '个股公告', industry: '行业动态',
                 international: '国际市场', other: '其他' };
  var SENTI_CN = { bullish: '利好', bearish: '利空', neutral: '中性' };
  var SLOT_CN = { am: '盘前', pm: '盘后' };
  // M1 原始新闻的分类（与 M2 的五分类不是一套）
  var RAW_CAT_CN = { finance: '财经', tech: '科技', policy: '政策',
                     official: '官方', other: '其他' };

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pct(v) {
    return (typeof v === 'number') ? (v >= 0 ? '+' : '') + v.toFixed(2) + '%' : '—';
  }
  function dirCls(v) {
    if (typeof v !== 'number') return 'flat';
    return v > 0 ? 'up' : (v < 0 ? 'down' : 'flat');
  }
  function toast(msg) {
    var t = $('#toast');
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.add('hidden'); }, 3200);
  }

  // ── 数据访问 ────────────────────────────────────────────
  /* 静态版的数据以 <script> 注入到 window.__DATA__，不走 fetch。
     浏览器的 CORS 策略禁止 file:// 页面发起 fetch，双击打开导出的
     index.html 时 fetch 会直接报 "Failed to fetch"，而 script 标签不受限。 */
  function loadStatic(name) {
    if (window.__DATA__ && window.__DATA__[name]) {
      return Promise.resolve(window.__DATA__[name]);
    }
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = 'api/' + name + '.js';
      s.onload = function () {
        if (window.__DATA__ && window.__DATA__[name]) resolve(window.__DATA__[name]);
        else reject(new Error('静态数据 ' + name + ' 为空'));
      };
      s.onerror = function () { reject(new Error('静态数据 ' + name + '.js 载入失败')); };
      document.head.appendChild(s);
    });
  }

  function apiGet(name, params) {
    if (STATIC) return loadStatic(name);
    var qs = params ? '?' + new URLSearchParams(params).toString() : '';
    return fetch('/api/' + name + qs, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (e) {
          throw new Error(e.error || ('HTTP ' + r.status));
        });
      }
      return r.json();
    });
  }

  function apiPost(name, body) {
    if (STATIC) return Promise.reject(new Error('静态版不支持 AI 追问，请运行本地服务'));
    return fetch('/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (e) {
          throw new Error(e.error || ('HTTP ' + r.status));
        });
      }
      return r.json();
    });
  }

  var S = {
    meta: null, overview: null, news: [], raw: [], rawLoaded: false,
    quotes: [], failed: [], boards: [], history: [], analysis: null, picks: null,
    rawMode: false, shown: PAGE, sort: { key: 'change_pct', asc: false }
  };

  var KIND_CN = { stock: '个股', board: '板块' };
  // 与 m10_picks/picks.py 的 _STATUS_CN / m5_report 保持一致
  var REV_STATUS_CN = { no_quote: '未取到行情', no_bench: '基准缺失', expired: '未取到行情' };
  var PICK_TIERS = [1, 3, 5];

  // ── 总览 ────────────────────────────────────────────────
  function renderOverview() {
    var o = S.overview;

    $('#conclusionText').textContent = o.conclusion || '今日暂无结论（M3 未产出）';

    var score = o.sentiment_score || 0;
    // 夹在 1.5%~98.5%：指针有宽度，贴边会被轨道裁掉一半
    var pos = Math.min(98.5, Math.max(1.5, (score + 1) / 2 * 100));
    $('#sentiGauge').innerHTML =
      '<div class="gauge-track"><div class="gauge-mark" style="left:' + pos + '%"></div></div>' +
      '<div class="gauge-legend"><span>偏空</span>' +
      '<span class="muted small">' + esc(o.directional_note || '') + '</span>' +
      '<span>偏多</span></div>';

    var v = o.verified || {};
    $('#statGrid').innerHTML = [
      stat(o.raw_count, '条原始新闻', 'M1 收集'),
      stat(o.structured_count, '条结构化', '丢弃 ' + (o.prefilter_dropped || 0) + ' 条'),
      stat(o.quotes.count, '只个股行情', o.quotes.failed ? (o.quotes.failed + ' 只未取到') : '全部成功'),
      stat((v.confirmed || 0), '条多源确认', (v.unverified || 0) + ' 条待核实')
    ].join('');

    var sn = o.sentiments || {};
    var tot = (sn.bullish || 0) + (sn.bearish || 0) + (sn.neutral || 0) || 1;
    $('#sentiDist').innerHTML = [
      distRow('利好', sn.bullish || 0, tot, 'bar-bull'),
      distRow('利空', sn.bearish || 0, tot, 'bar-bear'),
      distRow('中性', sn.neutral || 0, tot, 'bar-mid')
    ].join('');

    var cats = o.categories || {};
    var catOrder = ['policy', 'stock', 'industry', 'international', 'other'];
    var catMax = Math.max.apply(null, catOrder.map(function (k) { return cats[k] || 0; }).concat([1]));
    $('#catDist').innerHTML = catOrder.filter(function (k) { return cats[k]; }).map(function (k) {
      return distRow(CAT_CN[k] || k, cats[k], catMax, 'bar-accent');
    }).join('') || '<p class="empty">暂无数据</p>';

    $('#sourceList').innerHTML = (o.sources || []).map(function (s) {
      return '<div class="row"><span class="r-name">' + esc(s.name) + '</span>' +
        (s.ok ? '' : '<span class="badge unverified">失败</span>') +
        '<span class="r-spacer"></span>' +
        '<span class="muted small">' + (s.ok ? s.count + ' 条' : esc(s.error || '')) + '</span></div>';
    }).join('') || '<p class="empty">暂无数据</p>';

    $('#gainers').innerHTML = quoteRows(o.quotes.gainers, '今日无上涨个股');
    $('#losers').innerHTML = quoteRows(o.quotes.losers, '今日无下跌个股');

    $('#topBoards').innerHTML = (o.top_boards || []).map(function (b) {
      return '<button class="chip" data-board="' + esc(b.name) + '">' + esc(b.name) +
        '<span class="c">' + b.count + '</span></button>';
    }).join('') || '<p class="empty">暂无板块数据</p>';

    $('#brandSub').textContent = o.date + ' · 原始 ' + o.raw_count + ' 条 · 结构化 ' +
      o.structured_count + ' 条 · 行情 ' + o.quotes.count + ' 只';
    document.title = '股市情报 · ' + o.date;

    // 结论里点板块名不便于操作，这里统一由 chips 承担跳转
    $$('#topBoards .chip').forEach(function (el) {
      el.addEventListener('click', function () {
        switchTab('boards');
        setTimeout(function () { openBoard(el.dataset.board); }, 60);
      });
    });
  }

  function stat(v, k, note) {
    return '<div class="stat"><div class="v">' + v + '</div><div class="k">' + esc(k) +
      '</div><div class="note">' + esc(note || '') + '</div></div>';
  }
  function distRow(name, n, tot, cls) {
    var w = tot > 0 ? Math.round(n / tot * 100) : 0;
    return '<div class="dist-row"><span class="name">' + esc(name) + '</span>' +
      '<span class="bar"><i class="' + cls + '" style="width:' + w + '%"></i></span>' +
      '<span class="num">' + n + '</span></div>';
  }
  function quoteRows(list, emptyMsg) {
    if (!list || !list.length) return '<p class="empty">' + esc(emptyMsg || '暂无数据') + '</p>';
    return '<div class="row-list">' + list.map(function (q) {
      return '<div class="row"><span class="r-name">' + esc(q.name) +
        '<span class="r-code"> ' + esc(q.code) + '</span></span><span class="r-spacer"></span>' +
        '<span class="r-val muted small">' + (typeof q.price === 'number' ? q.price.toFixed(2) : '—') + '</span>' +
        '<span class="r-val ' + dirCls(q.change_pct) + '">' + pct(q.change_pct) + '</span></div>';
    }).join('') + '</div>';
  }

  // ── 新闻 ────────────────────────────────────────────────
  function currentSource() { return S.rawMode ? S.raw : S.news; }

  function filtered() {
    var q = $('#fq').value.trim().toLowerCase();
    var terms = q ? q.split(/\s+/).filter(Boolean) : [];
    var cat = $('#fcat').value, senti = $('#fsenti').value, veri = $('#fverified').value;
    var src = $('#fsource').value, board = $('#fboard').value, stock = $('#fstock').value;

    var out = currentSource().filter(function (n) {
      if (src && n.source !== src) return false;
      if (S.rawMode) {
        // 原始新闻没有 M2 的分类/情绪/板块字段，只按关键词与来源筛
        if (!terms.length) return true;
        var hay0 = (n.text || '').toLowerCase();
        return terms.every(function (t) { return hay0.indexOf(t) >= 0; });
      }
      if (cat && (n.category || 'other') !== cat) return false;
      if (senti && (n.sentiment || 'neutral') !== senti) return false;
      if (veri && (n.verified || 'unverified') !== veri) return false;
      if (board && (n.board || []).indexOf(board) < 0) return false;
      if (stock && (n.stocks || []).indexOf(stock) < 0) return false;
      if (!terms.length) return true;
      var hay = ((n.text || '') + ' ' + (n.stocks || []).join(' ') + ' ' +
                 (n.board || []).join(' ')).toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) >= 0; });
    });

    var sort = $('#fsort').value;
    if (sort === 'source') {
      out.sort(function (a, b) { return String(a.source).localeCompare(String(b.source), 'zh'); });
    } else if (sort === 'sentiment') {
      var rk = { bullish: 0, bearish: 1, neutral: 2 };
      out.sort(function (a, b) {
        return (rk[a.sentiment] == null ? 3 : rk[a.sentiment]) -
               (rk[b.sentiment] == null ? 3 : rk[b.sentiment]);
      });
    } else {
      out.sort(function (a, b) { return String(b.time).localeCompare(String(a.time)); });
    }
    return out;
  }

  function renderNews(reset) {
    if (reset) S.shown = PAGE;
    var list = filtered();
    var page = list.slice(0, S.shown);

    $('#newsCount').textContent = S.rawMode
      ? ('原始新闻 ' + list.length + ' 条（已显示 ' + page.length + ' 条）')
      : ('结构化新闻 ' + list.length + ' 条（已显示 ' + page.length + ' 条）');
    var slot = $('#newsPager');
    slot.innerHTML = S.rawMode ? '<span class="badge unverified">原始视图</span>' : '';

    $('#newsList').innerHTML = page.length
      ? page.map(function (n) { return newsCard(n, S.rawMode); }).join('')
      : '<p class="empty">没有符合条件的新闻</p>';
    $('#btnMore').classList.toggle('hidden', page.length >= list.length);
  }

  /* isRaw 显式传入，不能靠 item 上有没有 dropped 字段判断：
     原始新闻里"被 M2 选中"的那些 dropped 为 false，
     若据此当普通新闻渲染，点击会跳到 id 恰好相同的另一条结构化新闻。 */
  function newsCard(n, isRaw) {
    var meta = ['<span>' + esc((n.time || '').slice(5, 16)) + '</span>',
                '<span>' + esc(n.source || '') + '</span>'];
    if (isRaw) {
      meta.push('<span class="badge ' + (n.kept ? 'confirmed' : 'unverified') + '">' +
                (n.kept ? 'M2 已选中' : 'M2 未选中') + '</span>');
      if (n.category) meta.push('<span class="tag">' + esc(RAW_CAT_CN[n.category] || n.category) + '</span>');
    } else {
      if (n.category) meta.push('<span class="tag">' + esc(CAT_CN[n.category] || n.category) + '</span>');
      meta.push('<span class="badge ' + (n.verified === 'confirmed' ? 'confirmed' : 'unverified') + '">' +
                (n.verified === 'confirmed' ? '已确认' : '待核实') + '</span>');
      meta.push('<span class="badge ' + esc(n.sentiment || 'neutral') + '">' +
                esc(SENTI_CN[n.sentiment] || '中性') + '</span>');
      (n.board || []).forEach(function (b) {
        if (b && b !== '其他') meta.push('<span class="tag">' + esc(b) + '</span>');
      });
      (n.stocks || []).forEach(function (s) {
        meta.push('<span class="tag stock">' + esc(s) + '</span>');
      });
      meta.push('<span class="ni-act">详情 →</span>');
    }

    return '<article class="news-item' + (isRaw && !n.kept ? ' dropped' : '') + '"' +
      (isRaw ? '' : ' data-id="' + n.id + '"') + '>' +
      '<div class="ni-head">' +
      (!isRaw && typeof n.id === 'number' ? '<span class="ni-idx">' + n.id + '</span>' : '') +
      '<div class="ni-text">' + esc(n.text) + '</div></div>' +
      '<div class="ni-meta">' + meta.join('') + '</div></article>';
  }

  function fillSelects() {
    var cats = {}, srcs = {}, brds = {}, stks = {};
    S.news.forEach(function (n) {
      cats[n.category] = (cats[n.category] || 0) + 1;
      srcs[n.source] = (srcs[n.source] || 0) + 1;
      (n.board || []).forEach(function (b) { if (b && b !== '其他') brds[b] = (brds[b] || 0) + 1; });
      (n.stocks || []).forEach(function (s) { if (s) stks[s] = (stks[s] || 0) + 1; });
    });
    S.raw.forEach(function (n) { srcs[n.source] = (srcs[n.source] || 0) + 1; });

    function opts(map, label, cnMap) {
      return '<option value="">' + label + '</option>' + Object.keys(map)
        .sort(function (a, b) { return map[b] - map[a]; })
        .map(function (k) {
          return '<option value="' + esc(k) + '">' +
            esc((cnMap && cnMap[k]) || k) + '（' + map[k] + '）</option>';
        }).join('');
    }
    $('#fcat').innerHTML = opts(cats, '全部分类', CAT_CN);
    $('#fsource').innerHTML = opts(srcs, '全部来源');
    $('#fboard').innerHTML = opts(brds, '全部板块');
    $('#fstock').innerHTML = opts(stks, '全部个股');
  }

  // ── 行情 ────────────────────────────────────────────────
  function renderQuotes() {
    $('#quoteCount').textContent = S.quotes.length + ' 只';
    var list = S.quotes.slice().sort(function (a, b) {
      var k = S.sort.key, av = a[k], bv = b[k];
      if (typeof av === 'string' || typeof bv === 'string') {
        return String(av).localeCompare(String(bv), 'zh') * (S.sort.asc ? 1 : -1);
      }
      return ((av == null ? -Infinity : av) - (bv == null ? -Infinity : bv)) * (S.sort.asc ? 1 : -1);
    });
    $('#quoteTable tbody').innerHTML = list.map(function (q) {
      return '<tr><td><strong>' + esc(q.name) + '</strong><span class="muted small"> ' +
        esc(q.code) + '</span></td>' +
        '<td class="muted">' + esc(q.market || '') + '</td>' +
        '<td class="num">' + (typeof q.price === 'number' ? q.price.toFixed(2) : '—') + '</td>' +
        '<td class="num ' + dirCls(q.change_pct) + '">' + pct(q.change_pct) + '</td>' +
        '<td class="muted small">' + esc(q.source || '') + '</td></tr>';
    }).join('') || '<tr><td colspan="5" class="empty">暂无行情数据</td></tr>';

    $('#quoteFailed').textContent = S.failed.length
      ? '未取到行情：' + S.failed.map(function (f) { return f.name; }).join('、')
      : '';
  }

  // ── 板块 ────────────────────────────────────────────────
  function renderBoards() {
    $('#boardCount').textContent = '共 ' + S.boards.length + ' 个板块（按新闻条数排序）';
    $('#boardGrid').innerHTML = S.boards.map(function (b) {
      var s = b.sentiments || {};
      var tot = (s.bullish || 0) + (s.bearish || 0) + (s.neutral || 0) || 1;
      var mini = [['bullish', 'bar-bull'], ['neutral', 'bar-mid'], ['bearish', 'bar-bear']]
        .map(function (p) {
          var w = Math.round((s[p[0]] || 0) / tot * 100);
          return w ? '<i class="' + p[1] + '" style="width:' + w + '%"></i>' : '';
        }).join('');
      return '<div class="board-card" data-board="' + esc(b.name) + '">' +
        '<div class="bc-head"><span class="bc-name">' + esc(b.name) + '</span>' +
        '<span class="bc-count">' + b.count + ' 条 · 利好 ' + (s.bullish || 0) +
        ' / 利空 ' + (s.bearish || 0) + '</span></div>' +
        '<div class="bc-mini">' + mini + '</div>' +
        '<div class="bc-stocks">' + (b.stocks || []).slice(0, 8).map(function (x) {
          return '<span class="tag stock">' + esc(x) + '</span>';
        }).join('') + '</div></div>';
    }).join('') || '<p class="empty">暂无板块数据</p>';

    $$('#boardGrid .board-card').forEach(function (el) {
      el.addEventListener('click', function () { openBoard(el.dataset.board); });
    });
  }

  function openBoard(name) {
    switchTab('news');
    $('#fclear').click();
    $('#fboard').value = name;
    if ($('#fboard').value !== name) { $('#fq').value = name; }
    renderNews(true);
    $('#panel-news').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ── 分析 ────────────────────────────────────────────────
  function renderAnalysis() {
    if (!S.analysis) return;
    $('#analysisBody').innerHTML = S.analysis.html || '<p class="empty">今日暂无分析</p>';

    var ex = ['今天最值得关注的消息是什么？'];
    (S.analysis.market_view || []).slice(0, 3).forEach(function (m) {
      ex.push(m.name + ' 板块今天有什么消息？');
    });
    ex.push('今天有哪些个股公告需要特别留意？');
    $('#askExamples').innerHTML = ex.slice(0, 5).map(function (t) {
      return '<button class="chip">' + esc(t) + '</button>';
    }).join('');
    $$('#askExamples .chip').forEach(function (el) {
      el.addEventListener('click', function () { $('#askInput').value = el.textContent; ask(); });
    });

    if (!S.meta.ask_enabled) {
      $('#askHint').innerHTML = '<strong>AI 追问当前不可用</strong>：' +
        (STATIC ? '静态版没有后端，请运行 <code>python modules/m9_web/server.py</code> 后使用。'
                : '未设置环境变量 <code>DEEPSEEK_API_KEY</code>，设置后重启服务即可。');
      $('#btnAsk').disabled = true;
    }
  }

  function ask() {
    var q = $('#askInput').value.trim();
    if (!q) { toast('请先输入问题'); return; }
    $('#btnAsk').disabled = true;
    $('#askStatus').innerHTML = '<span class="spin">◐</span> 正在基于今日新闻分析…';
    $('#askAnswer').classList.add('hidden');

    apiPost('ask', { question: q }).then(function (r) {
      $('#askAnswer').innerHTML = r.answer_html +
        '<div class="used">本次依据 ' + r.count + ' 条新闻，引用编号 ' +
        (r.used_news || []).slice(0, 24).map(function (c) {
          return '<a class="cite" href="#news-' + c + '" data-news-id="' + c + '">' + c + '</a>';
        }).join('') + '</div>';
      $('#askAnswer').classList.remove('hidden');
      $('#askStatus').textContent = '';
    }).catch(function (e) {
      $('#askStatus').textContent = '';
      toast('追问失败：' + e.message);
    }).then(function () {
      $('#btnAsk').disabled = !S.meta.ask_enabled;
    });
  }

  // ── 历史 ────────────────────────────────────────────────
  function reportUrl(file) { return STATIC ? ('../' + file) : ('/reports/' + file); }

  function renderHistory() {
    if (!S.history.length) {
      $('#historyList').innerHTML = '<p class="empty">暂无历史日报</p>';
      return;
    }
    $('#historyList').innerHTML = S.history.map(function (d) {
      return '<div class="hist-day"><span class="hist-date">' + esc(d.date) + '</span>' +
        '<span class="hist-links">' + d.entries.map(function (e) {
          return '<a class="btn ghost" href="' + esc(reportUrl(e.file)) + '" target="_blank">' +
            esc(e.label) + ' ↗</a>';
        }).join('') + '</span></div>';
    }).join('');
  }

  // ── 候选观察清单（M10） ──────────────────────────────────
  /* 不显示「胜率」「命中率」，也不给超额加红绿配色：超额 +0.1% 与 -0.1%
     经济上几乎没有差别，用颜色把它们分成两档会凭空造出「对/错」的观感。
     均值与**样本数**永远一起出现——样本 3 条时的均值不配单独示人。 */
  function revCell(rev) {
    if (!rev) return '<td class="num pick-pending">—</td>';
    if (rev.status !== 'ok') {
      return '<td class="num pick-pending">' + esc(REV_STATUS_CN[rev.status] || '未取到行情') +
        '</td>';
    }
    var lag = (rev.done && rev.due && rev.done !== rev.due)
      ? '<span class="pick-lag" title="计划 ' + esc(rev.due) + '，实际在 ' + esc(rev.done) +
        ' 取到行情">补</span>' : '';
    var a = (typeof rev.alpha === 'number')
      ? '<b>' + pct(rev.alpha * 100) + '</b>'
      : '<span class="muted">—</span>';
    return '<td class="num">' + pct(rev.ret * 100) +
      '<div class="pick-alpha">超额 ' + a + '</div>' + lag + '</td>';
  }

  function pickCard(r) {
    var conf = r.confidence ? '<span class="pick-conf">置信度 ' + esc(r.confidence) + '</span>' : '';
    var board = r.board ? '<span class="pick-tag">' + esc(r.board) + '</span>' : '';
    var refs = (r.basis_ids || []).filter(function (i) { return i < S.news.length; });
    var ref = refs.length
      ? '<span class="pick-ref">依据 ' + refs.map(function (i) {
          return '<a class="cite" href="#news-' + i + '" data-news-id="' + i + '">' + i + '</a>';
        }).join(' ') + '</span>'
      : '';
    var base = (typeof r.base_price === 'number')
      ? '<span class="pick-base">记录时价 ' + r.base_price + '</span>' : '';
    return '<div class="pick-card">' +
      '<div class="pick-head"><b>' + esc(r.name) + '</b>' +
      '<span class="pick-kind">' + esc(KIND_CN[r.kind] || r.kind) + '</span>' +
      board + conf + '</div>' +
      (r.logic ? '<p class="pick-logic">' + esc(r.logic) + '</p>' : '') +
      (r.invalidation
        ? '<p class="pick-inval"><b>推翻条件</b>' + esc(r.invalidation) + '</p>' : '') +
      '<div class="pick-foot">' + base + ref + '</div></div>';
  }

  function pickRow(r) {
    return '<tr><td class="pick-date">' + esc(r.date) +
      '<span class="muted small">' + esc(SLOT_CN[r.slot] || r.slot || '') + '</span></td>' +
      '<td>' + esc(r.name) +
      '<span class="muted small">' + esc(KIND_CN[r.kind] || '') +
      (r.board ? ' · ' + esc(r.board) : '') + '</span></td>' +
      PICK_TIERS.map(function (k) { return revCell((r.reviews || {})[String(k)]); }).join('') +
      '</tr>';
  }

  function renderPicks() {
    var p = S.picks || {};
    var rows = p.rows || [];
    if (!rows.length) {
      $('#picksBody').innerHTML =
        '<p class="pick-empty">还没有候选记录。候选在每次收盘后由 M10 写入，' +
        '次日及之后回填 T+1/T+3/T+5 的表现。</p>';
      return;
    }

    var stats = p.stats || {};
    var statParts = PICK_TIERS.map(function (k) {
      var s = stats[String(k)];
      if (!s || !s.n) return '';
      var mean = (typeof s.alpha === 'number')
        ? '<b>' + pct(s.alpha * 100) + '</b>' : '<span class="muted">—</span>';
      return '<span class="pick-stat">T+' + k + ' 均超额 ' + mean +
        '<span class="muted">（样本 ' + s.n + ' 条）</span></span>';
    }).filter(Boolean).join('');

    var today = (p.latest_date || '').trim();
    var todayRows = rows.filter(function (r) { return r.date === today; });
    // 盘前那次运行时当日还没有候选，此时 latest_date 是上一交易日 —— 如实标日期，
    // 不把它说成「今日」
    var cards = todayRows.length
      ? '<h3 class="pick-sub">' + esc(today) + ' 记录的候选（' + todayRows.length + ' 条）</h3>' +
        '<div class="pick-cards">' + todayRows.map(pickCard).join('') + '</div>'
      : '<p class="pick-empty">这次运行时还没有新的候选记录。</p>';

    $('#picksBody').innerHTML =
      '<div class="pick-stats">' + (statParts ||
        '<span class="muted small">还没有到期回填的表现数据</span>') + '</div>' +
      cards +
      '<h3 class="pick-sub">历史明细（含跑输的，共 ' + p.total + ' 条）</h3>' +
      '<div class="table-wrap"><table class="tbl pick-tbl"><thead><tr>' +
      '<th>记录日</th><th>候选</th><th>T+1</th><th>T+3</th><th>T+5</th>' +
      '</tr></thead><tbody>' + rows.map(pickRow).join('') + '</tbody></table></div>' +
      '<p class="muted small">超额 = 该候选涨跌幅 − 同期沪深300 涨跌幅；' +
      '收益率为未复权口径，除权除息期间会有偏差。' +
      '「补」表示该档实际取价日迟于计划日期（周末/停牌/休市）。</p>';
  }

  // ── 详情弹层 ────────────────────────────────────────────
  /* 关联新闻与行情都在浏览器端算：数据本来就全在手上，
     少一次往返，也让静态导出不必为每条新闻各生成一个文件。 */
  function showDetail(id) {
    var d = S.news[id];
    if (!d) { toast('新闻不存在'); return; }

    var boards = d.board || [], stocks = d.stocks || [];
    var related = [];
    for (var j = 0; j < S.news.length && related.length < 12; j++) {
      if (j === id) continue;
      var o = S.news[j];
      var hit = (o.board || []).some(function (b) { return boards.indexOf(b) >= 0; }) ||
                (o.stocks || []).some(function (s) { return stocks.indexOf(s) >= 0; });
      if (hit) related.push(j);
    }
    var matched = S.quotes.filter(function (q) {
      return stocks.indexOf(q.name) >= 0 || stocks.indexOf(q.matched_by) >= 0;
    });

    $('#modalTitle').textContent = '新闻详情 #' + id;
    var h = [];
    h.push('<div class="markdown"><p>' + esc(d.text) + '</p></div>');
    h.push('<div class="sec-label">元信息</div>');
    h.push('<div class="ni-meta">' +
      '<span class="tag">' + esc(CAT_CN[d.category] || d.category || '') + '</span>' +
      '<span class="badge ' + (d.verified === 'confirmed' ? 'confirmed' : 'unverified') + '">' +
      (d.verified === 'confirmed' ? '已确认（多源）' : '待核实（单源）') + '</span>' +
      '<span class="badge ' + esc(d.sentiment || 'neutral') + '">' +
      esc(SENTI_CN[d.sentiment] || '中性') + '</span>' +
      '<span>' + esc(d.time || '') + '</span><span>' + esc(d.source || '') + '</span>' +
      (d.url ? '<a href="' + esc(d.url) + '" target="_blank" rel="noopener">原文 ↗</a>' : '') +
      '</div>');

    if (matched.length) {
      h.push('<div class="sec-label">相关行情</div>');
      h.push(quoteRows(matched));
    }
    if (related.length) {
      h.push('<div class="sec-label">同板块 / 同个股的其他新闻</div>');
      h.push('<div class="row-list">' + related.map(function (rid) {
        var n = S.news[rid];
        return '<div class="row" style="cursor:pointer" data-goto="' + rid + '">' +
          '<span class="r-name">' + esc((n.text || '').slice(0, 46)) + '…</span>' +
          '<span class="r-spacer"></span><span class="muted small">' + esc(n.source) + '</span></div>';
      }).join('') + '</div>');
    }
    $('#modalBody').innerHTML = h.join('');
    $('#modalMask').classList.remove('hidden');
  }

  // ── 标签页 ──────────────────────────────────────────────
  function switchTab(name) {
    $$('.tab').forEach(function (t) { t.classList.toggle('active', t.dataset.tab === name); });
    $$('.panel').forEach(function (p) {
      p.classList.toggle('hidden', p.id !== 'panel-' + name);
    });
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  }

  // ── 事件绑定 ────────────────────────────────────────────
  function bind() {
    $$('.tab').forEach(function (t) {
      t.addEventListener('click', function () { switchTab(t.dataset.tab); });
    });

    ['fq', 'fsort', 'fcat', 'fsenti', 'fverified', 'fsource', 'fboard', 'fstock']
      .forEach(function (id) {
        var el = $('#' + id);
        el.addEventListener(id === 'fq' ? 'input' : 'change', function () { renderNews(true); });
      });
    var deb;
    $('#fq').addEventListener('input', function () {
      clearTimeout(deb); deb = setTimeout(function () { renderNews(true); }, 180);
    });

    $('#fclear').addEventListener('click', function () {
      ['fq', 'fsort', 'fcat', 'fsenti', 'fverified', 'fsource', 'fboard', 'fstock']
        .forEach(function (id) { var el = $('#' + id); el.value = id === 'fsort' ? 'time' : ''; });
      renderNews(true);
    });

    $('#fraw').addEventListener('change', function () {
      var on = this.checked;
      S.rawMode = on;
      ['fcat', 'fsenti', 'fverified', 'fboard', 'fstock'].forEach(function (id) {
        $('#' + id).disabled = on;
      });
      // 原始新闻有 1000+ 条、体积远超其余数据之和，用到时才拉
      if (on && !S.rawLoaded) {
        $('#newsCount').textContent = '正在载入原始新闻…';
        apiGet('raw').then(function (r) {
          S.raw = r.items || [];
          S.rawLoaded = true;
          fillSelects();
          renderNews(true);
        }).catch(function (e) {
          toast('原始新闻载入失败：' + e.message);
          renderNews(true);
        });
      } else {
        renderNews(true);
      }
    });

    $('#btnMore').addEventListener('click', function () {
      S.shown += PAGE; renderNews(false);
    });

    $('#newsList').addEventListener('click', function (e) {
      var card = e.target.closest('.news-item');
      if (!card || !card.dataset.id) return;
      showDetail(parseInt(card.dataset.id, 10));
    });

    $('#analysisBody').addEventListener('click', function (e) {
      var a = e.target.closest('a.cite');
      if (!a) return;
      e.preventDefault();
      showDetail(parseInt(a.dataset.newsId, 10));
    });
    $('#askAnswer').addEventListener('click', function (e) {
      var a = e.target.closest('a.cite');
      if (!a) return;
      e.preventDefault();
      showDetail(parseInt(a.dataset.newsId, 10));
    });
    $('#picksBody').addEventListener('click', function (e) {
      var a = e.target.closest('a.cite');
      if (!a) return;
      e.preventDefault();
      showDetail(parseInt(a.dataset.newsId, 10));
    });

    $('#modalBody').addEventListener('click', function (e) {
      var row = e.target.closest('[data-goto]');
      if (!row) return;
      showDetail(parseInt(row.dataset.goto, 10));
    });
    $('#modalClose').addEventListener('click', function () {
      $('#modalMask').classList.add('hidden');
    });
    $('#modalMask').addEventListener('click', function (e) {
      if (e.target === this) this.classList.add('hidden');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') $('#modalMask').classList.add('hidden');
    });

    $$('#quoteTable thead th').forEach(function (th) {
      th.addEventListener('click', function () {
        var k = th.dataset.k;
        if (S.sort.key === k) S.sort.asc = !S.sort.asc;
        else { S.sort.key = k; S.sort.asc = false; }
        renderQuotes();
      });
    });

    $('#btnAsk').addEventListener('click', ask);
    $('#askInput').addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') ask();
    });

    $('#btnRefresh').addEventListener('click', function () { load(true); });
  }

  // ── 启动 ────────────────────────────────────────────────
  function load(isReload) {
    $('#btnRefresh').disabled = true;
    return Promise.all([
      apiGet('meta'), apiGet('overview'),
      apiGet('news', { limit: 2000 }),
      apiGet('quotes'), apiGet('boards'),
      apiGet('analysis'), apiGet('history'),
      apiGet('picks')                    // 新项一律追加在末尾：中间插入要顺移所有下标
    ]).then(function (r) {
      S.meta = r[0]; S.overview = r[1]; S.news = r[2].items || [];
      S.quotes = r[3].quotes || []; S.failed = r[3].failed || [];
      S.boards = r[4].boards || []; S.analysis = r[5]; S.history = r[6].reports || [];
      S.picks = r[7] || null;
      S.raw = []; S.rawLoaded = false;   // 原始新闻按需再拉

      fillSelects();
      renderOverview(); renderNews(true); renderQuotes();
      renderBoards(); renderAnalysis(); renderHistory(); renderPicks();
      if (isReload) toast('已重新载入数据');
    }).catch(function (e) {
      $('#conclusionText').textContent = '数据加载失败';
      $('#brandSub').textContent = e.message;
      toast('加载失败：' + e.message);
    }).then(function () {
      $('#btnRefresh').disabled = false;
    });
  }

  bind();
  var initial = (location.hash || '').replace('#', '');
  if (initial && $('#panel-' + initial)) switchTab(initial);
  load(false);
})();
