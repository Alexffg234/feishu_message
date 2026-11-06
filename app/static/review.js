/* ==========================================================================
   标签复核页 · 人工闭环的唯一入口
   - 队列:未分类组 + 低置信组(GET /api/review/queue),未分类优先
   - 定标:每卡片一个标签框(POST /api/review/apply),回车即提交
   - gold set:人工确认过的组(GET /api/review/gold),是唯一可信的准确率基线
   - 导出 CSV:字段与 scripts/topic_review.py --export 完全一致,可被
     `--csv --apply` 直接吃回,适合「先在页面填一遍、再批量写库」
   依赖 components.css 的设计令牌,不重复定义 :root。
   ========================================================================== */
'use strict';

var STATE = {
  chat: '',
  lowConf: 0.5,
  queue: { total: 0, items: [] },
  gold: null,
  // 本页已定标的组(chat|root):卡片会淡出移除,统计里也不能再算进「待复核」
  done: {},
  // 只有 admin 能调 /api/review/apply;非管理员进页面就该看到只读态,
  // 否则填完标签才被 403 拒绝,白干一场
  readonly: false
};

function keyOf(chat, root) { return String(chat) + '|' + String(root); }

/* ---------------------------------------------------------------- 工具 */
function esc(t) {
  var d = document.createElement('div');
  d.textContent = (t == null ? '' : String(t));
  return d.innerHTML;
}

// 毫秒时间戳字符串 -> 'MM-DD HH:mm';不可解析原样返回
function fmtMs(ms) {
  var n = Number(ms);
  if (!isFinite(n) || n <= 0) { return '—'; }
  var d = new Date(n);
  var p = function (v) { return ('0' + v).slice(-2); };
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

// 根 ID 一律保留尾部:om_xxx 类 ID 前缀相同,只截前面会让所有卡片看起来一模一样
function shortId(id) {
  var s = String(id || '');
  if (s.length <= 16) { return s; }
  return '…' + s.slice(-12);
}

function pct(v) {
  return (Number(v || 0) * 100).toFixed(1) + '%';
}

var toastTimer = null;
function toast(msg, kind) {
  var box = document.getElementById('toast');
  box.textContent = msg;
  box.className = 'rv-toast' + (kind ? ' rv-toast--' + kind : '');
  box.hidden = false;
  if (toastTimer) { clearTimeout(toastTimer); }
  toastTimer = setTimeout(function () { box.hidden = true; }, kind === 'error' ? 6000 : 3800);
}

// FastAPI 的 detail 可能是字符串,也可能是校验错误数组
function errText(payload, fallback) {
  if (!payload) { return fallback; }
  var d = payload.detail;
  if (typeof d === 'string') { return d; }
  if (Array.isArray(d) && d.length) { return d.map(function (x) { return x.msg || JSON.stringify(x); }).join('; '); }
  return fallback;
}

/* ---------------------------------------------------------------- 标准标签 */
// 大类树 -> datalist(按大类分组,方便在大类语境下挑小类)
function buildStandards(groups) {
  var datalist = document.getElementById('stdLabels');
  var tops = [], byName = {};
  (groups || []).forEach(function (g) {
    if (!g.parent) {
      tops.push(g);
      byName[g.name] = { name: g.name, topics: (g.topics || []).slice() };
    }
  });
  var orphans = [];
  (groups || []).forEach(function (g) {
    if (!g.parent) { return; }
    if (byName[g.parent]) {
      byName[g.parent].topics = byName[g.parent].topics.concat(g.topics || []);
    } else {
      orphans.push(g);
    }
  });

  function addGroup(label, topics) {
    var seen = {};
    var opts = (topics || []).filter(function (t) {
      t = (t || '').trim();
      if (!t || seen[t]) { return false; }
      seen[t] = 1;
      return true;
    });
    if (!opts.length) { return; }
    var og = document.createElement('optgroup');
    og.label = label;
    opts.forEach(function (t) {
      var o = document.createElement('option');
      o.value = t;
      og.appendChild(o);
    });
    datalist.appendChild(og);
  }

  tops.forEach(function (t) { addGroup(t.name, byName[t.name].topics); });
  orphans.forEach(function (g) { addGroup(g.parent + ' / ' + g.name, g.topics); });

  var n = datalist.querySelectorAll('option').length;
  if (!n) {
    // 大类没有配置时补一句,避免输入框看起来「什么都不会提示」
    datalist.setAttribute('data-empty', '1');
  }
  return n;
}

/* ---------------------------------------------------------------- 统计 */
function renderStats() {
  var q = STATE.queue;
  var items = (q.items || []).filter(function (i) {
    return !STATE.done[keyOf(i.chat_id, i.root_message_id)];
  });
  var unclassified = items.filter(function (i) { return i.reason === 'unclassified'; }).length;
  var lowConf = items.length - unclassified;
  var gold = STATE.gold || {};
  var shown = items.length;
  var truncated = (q.total || 0) > shown;

  var cards = [
    { num: q.total || 0, lbl: '待复核组数', hint: 'pending 不降 = 缺类或提示词不到位' },
    { num: unclassified, lbl: '本页未分类', hint: '闭集拒掉的都在这儿,不盯就静默堆积' },
    { num: lowConf, lbl: '本页低置信', hint: '低于 ' + STATE.lowConf + ' 的组' },
    { num: gold.gold_groups || 0, lbl: 'gold 组数', hint: '人工确认过 · 共 ' + (gold.total_groups || 0) + ' 组' },
    { num: pct(gold.human_rate), lbl: '人工覆盖率', hint: 'gold_groups=0 时所有准确率都是自说自话' }
  ];

  document.getElementById('stats').innerHTML = cards.map(function (c) {
    return '<div class="rv-stat">'
      + '<div class="rv-stat__num">' + esc(c.num) + '</div>'
      + '<div class="rv-stat__lbl">' + esc(c.lbl) + '</div>'
      + '<div class="rv-stat__hint">' + esc(c.hint) + '</div>'
      + '</div>';
  }).join('') + (truncated
    ? '<div class="rv-stat rv-stat--warn">'
      + '<div class="rv-stat__num">' + shown + '</div>'
      + '<div class="rv-stat__lbl">本页已列出</div>'
      + '<div class="rv-stat__hint">队列被截断,先处理完这批再刷新</div>'
      + '</div>'
    : '');
}

/* ---------------------------------------------------------------- 队列 */
function itemHtml(it, idx) {
  var unclassified = it.reason === 'unclassified';
  var badge = unclassified
    ? '<span class="badge badge--danger">未分类</span>'
    : '<span class="badge badge--warn">低置信 ' + it.topic_conf.toFixed(2) + '</span>';
  var cur = it.topic
    ? '现值 <b>' + esc(it.topic) + '</b>'
    : '<span class="rv-item__cur--none">闭集未给出标签</span>';

  var samples = (it.samples && it.samples.length)
    ? it.samples.map(function (s) {
      return '<div class="rv-sample" title="' + esc(s) + '">' + esc(s) + '</div>';
    }).join('')
    : '<div class="rv-sample rv-sample--none">(无文本样本,可能是纯图片/文件消息)</div>';

  return '<article class="rv-item" data-idx="' + idx + '"'
    + ' data-chat="' + esc(it.chat_id) + '"'
    + ' data-root="' + esc(it.root_message_id) + '">'
    + '<header class="rv-item__head">'
    + badge
    + '<span class="rv-item__meta">' + it.message_count + ' 条 · '
    + esc(fmtMs(it.last_time)) + ' · 根 ' + esc(shortId(it.root_message_id)) + '</span>'
    + '<span class="rv-item__cur">' + cur + '</span>'
    + '</header>'
    + '<div class="rv-item__samples">' + samples + '</div>'
    + '<div class="rv-item__act">'
    + '<input class="rv-input rv-label" list="stdLabels" autocomplete="off"'
    + ' placeholder="标准标签 / 近似词,回车确认">'
    + '<button class="btn rv-ok" type="button">确认</button>'
    + '<label class="rv-chk"><input type="checkbox" class="rv-prop"> 连带回刷同错标签</label>'
    + '<label class="rv-chk"><input type="checkbox" class="rv-allow"> 允许闭集外</label>'
    + '<span class="rv-msg"></span>'
    + '</div>'
    + '</article>';
}

function renderQueue() {
  var box = document.getElementById('queue');
  // 保留原始下标:导出 CSV 时要用它回查 samples / topic_conf
  var items = (STATE.queue.items || []).map(function (it, idx) {
    return { it: it, idx: idx };
  }).filter(function (pair) {
    return !STATE.done[keyOf(pair.it.chat_id, pair.it.root_message_id)];
  });
  if (!items.length) {
    box.innerHTML = '<div class="card rv-empty">'
      + '<div class="rv-empty__t">复核队列为空</div>'
      + '<div class="rv-empty__s">没有未分类组,也没有低于 ' + STATE.lowConf + ' 的低置信组。'
      + '这通常说明闭集覆盖率够用;若刚跑过批量定标,记得用 <code>--eval</code> 重跑 gold set 看准确率有没有真的变好。</div>'
      + '</div>';
    return;
  }
  box.innerHTML = items.map(function (pair) { return itemHtml(pair.it, pair.idx); }).join('');
  Array.prototype.forEach.call(box.querySelectorAll('.rv-item'), bindCard);
}

function bindCard(card) {
  var input = card.querySelector('.rv-label');
  var btn = card.querySelector('.rv-ok');
  if (STATE.readonly) {
    input.disabled = true;
    input.placeholder = '只读(需管理员)';
    btn.disabled = true;
    btn.textContent = '只读';
    // 连带回刷 / 允许闭集外只在提交时才有意义,只读态一并禁用免得看着能点
    Array.prototype.forEach.call(card.querySelectorAll('.rv-chk input'), function (c) {
      c.disabled = true;
    });
    return;
  }
  btn.addEventListener('click', function () { applyOne(card); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      applyOne(card);
    }
  });
}

async function applyOne(card) {
  var input = card.querySelector('.rv-label');
  var btn = card.querySelector('.rv-ok');
  if (btn.disabled) { return; }          // 同一张卡片正在提交,忽略连点
  var label = (input.value || '').trim();
  if (!label) {
    input.focus();
    toast('先填一个标准标签', 'error');
    return;
  }
  var body = {
    chat_id: card.getAttribute('data-chat'),
    root_message_id: card.getAttribute('data-root'),
    label: label,
    allow_new: !!card.querySelector('.rv-allow').checked,
    propagate: !!card.querySelector('.rv-prop').checked
  };

  var msg = card.querySelector('.rv-msg');
  btn.disabled = true;
  btn.textContent = '提交中…';
  msg.className = 'rv-msg';
  msg.textContent = '';

  try {
    var r = await fetch('/api/review/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body)
    });
    var payload = null;
    try { payload = await r.json(); } catch (e) { payload = null; }
    if (!r.ok) {
      msg.className = 'rv-msg rv-msg--error';
      if (r.status === 401) {
        msg.textContent = '登录已过期,请重新登录';
        toast('登录已过期,正在跳转登录页…', 'error');
        setTimeout(goLogin, 1200);
        return;
      }
      msg.textContent = errText(payload, 'HTTP ' + r.status);
      toast('拒绝:' + msg.textContent, 'error');
      return;                           // finally 会把按钮恢复可用,可以直接改完再提交
    }

    // 成功:汇报实际做了什么(旧值 -> 新值、别名、连带回刷),再把卡片移出队列
    var parts = [(payload.old_topic || '(空)') + ' → ' + payload.label];
    parts.push('影响 ' + payload.messages + ' 条');
    if (payload.aliases_written && payload.aliases_written.length) {
      parts.push('学别名 ' + payload.aliases_written.join('、'));
    } else if (payload.alias_skipped) {
      parts.push('未写别名:' + payload.alias_skipped);
    }
    if (payload.propagated_messages) {
      parts.push('连带回刷 ' + payload.propagated_messages + ' 条');
    }
    toast('已定标 ' + parts.join(' · '), 'ok');

    STATE.done[keyOf(body.chat_id, body.root_message_id)] = 1;
    STATE.queue.total = Math.max(0, (STATE.queue.total || 0) - 1);
    card.classList.add('rv-item--done');
    // 淡出结束后再摘掉节点;此时统计已按 STATE.done 过滤,数字与列表始终一致
    renderStats();
    setTimeout(function () {
      if (card.parentNode) { card.parentNode.removeChild(card); }
      if (!document.querySelectorAll('#queue .rv-item').length) { renderQueue(); }
    }, 420);

    // gold 组数变了,静默拉一次统计
    loadGold(true);
  } catch (e) {
    msg.className = 'rv-msg rv-msg--error';
    msg.textContent = '请求失败:' + e;
    toast('请求失败:' + e, 'error');
  } finally {
    if (btn.isConnected) {
      btn.disabled = false;
      btn.textContent = '确认';
    }
  }
}

/* ---------------------------------------------------------------- CSV */
// 字段与 scripts/topic_review.py --export 完全一致:label 列留空=跳过,
// 页面里已填但还没提交的值会带上,方便「先批量填、再交给脚本 --csv --apply」。
function exportCsv() {
  var cards = document.querySelectorAll('#queue .rv-item');
  if (!cards.length) {
    toast('队列为空,没有可导出的组', 'error');
    return;
  }
  var rows = [['chat_id', 'root_message_id', 'reason', 'current_topic', 'confidence',
    'message_count', 'label', 'sample']];
  Array.prototype.forEach.call(cards, function (card) {
    var idx = Number(card.getAttribute('data-idx'));
    var it = STATE.queue.items[idx] || {};
    rows.push([
      card.getAttribute('data-chat'),
      card.getAttribute('data-root'),
      it.reason || '',
      it.topic || '',
      it.topic_conf != null ? it.topic_conf.toFixed(3) : '',
      it.message_count != null ? it.message_count : '',
      (card.querySelector('.rv-label').value || '').trim(),
      (it.samples || []).join(' / ').slice(0, 200)
    ]);
  });

  var cell = function (v) {
    var s = (v == null ? '' : String(v));
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  var text = rows.map(function (r) { return r.map(cell).join(','); }).join('\r\n');
  // BOM:Excel 直接双击打开不乱码
  var blob = new Blob(['\ufeff' + text], { type: 'text/csv;charset=utf-8' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  var stamp = new Date().toISOString().slice(0, 10);
  a.href = url;
  a.download = 'review-' + stamp + '.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  toast('已导出 ' + (rows.length - 1) + ' 组;填好 label 列后 '
    + 'python scripts/topic_review.py --csv <文件名> --apply', 'ok');
}

/* ---------------------------------------------------------------- 加载 */
async function loadQueue() {
  var box = document.getElementById('queue');
  var stamp = document.getElementById('stamp');
  box.innerHTML = '<div class="loading">加载中…</div>';
  stamp.textContent = '加载中…';
  STATE.done = {};                      // 重新拉队列,本页「已定标」标记清空
  var url = '/api/review/queue?limit=100&low_conf=' + STATE.lowConf
    + (STATE.chat ? '&chat_id=' + encodeURIComponent(STATE.chat) : '');
  try {
    var r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) { throw new Error('HTTP ' + r.status); }
    STATE.queue = await r.json();
    renderStats();
    renderQueue();
    stamp.textContent = '更新于 ' + new Date().toLocaleTimeString();
  } catch (e) {
    box.innerHTML = '<div class="card rv-empty"><div class="rv-empty__t">加载失败</div>'
      + '<div class="rv-empty__s">' + esc(e.message || e) + '</div></div>';
    stamp.textContent = '加载失败';
  }
}

async function loadGold(silent) {
  var url = '/api/review/gold' + (STATE.chat ? '?chat_id=' + encodeURIComponent(STATE.chat) : '');
  try {
    var r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) { throw new Error('HTTP ' + r.status); }
    STATE.gold = await r.json();
  } catch (e) {
    if (!silent) { toast('gold set 加载失败:' + e, 'error'); }
    return;
  }
  renderStats();
  renderGold();
}

function renderGold() {
  var body = document.getElementById('goldBody');
  var gold = STATE.gold || {};
  var labels = gold.labels || {};
  var keys = Object.keys(labels);
  if (!gold.gold_groups) {
    body.innerHTML = '<div class="rv-empty__s">gold set 还是空的。'
      + '先在队列里定标几组——<strong>没有 gold set,任何「准确率」都只是模型自说自话</strong>。</div>';
    return;
  }
  var max = keys.reduce(function (a, k) { return Math.max(a, labels[k]); }, 1);
  body.innerHTML = '<div class="rv-gold__sum">'
    + gold.gold_groups + ' 组 / 全部 ' + gold.total_groups + ' 组 · 人工覆盖率 '
    + pct(gold.human_rate) + '</div>'
    + '<div class="rv-gold__list">'
    + keys.map(function (k) {
      var w = Math.max(2, Math.round(labels[k] / max * 100));
      return '<div class="rv-gold__row">'
        + '<span class="rv-gold__label" title="' + esc(k) + '">' + esc(k) + '</span>'
        + '<span class="rv-gold__bar"><i style="width:' + w + '%"></i></span>'
        + '<span class="rv-gold__n">' + labels[k] + '</span>'
        + '</div>';
    }).join('')
    + '</div>'
    + '<div class="rv-gold__tip">评估准确率需在服务器执行:'
    + '<code>python scripts/topic_review.py --gold --eval</code>(需配置 LLM)</div>';
}

async function loadStandards() {
  try {
    var r = await fetch('/api/topic_groups', { credentials: 'same-origin' });
    if (!r.ok) { return; }
    var d = await r.json();
    buildStandards(d.groups || []);
  } catch (e) { /* 标签提示拿不到不影响定标 */ }
}

function goLogin() {
  var next = location.pathname + location.search;
  location.assign('/login?next=' + encodeURIComponent(next));
}

// 角色决定能不能定标:非 admin 直接进只读态(接口侧同样会拦,这里只是少一次白填)
async function loadRole() {
  try {
    var r = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (!r.ok) { return; }              // 未启用登录时这里会 401,保持可编辑
    var u = await r.json();
    STATE.readonly = !(u && u.role === 'admin');
  } catch (e) {
    return;
  }
  if (STATE.readonly) {
    document.getElementById('roBar').hidden = false;
    Array.prototype.forEach.call(document.querySelectorAll('#queue .rv-item'), bindCard);
  }
}

function readControls() {
  STATE.chat = (document.getElementById('chatId').value || '').trim();
  STATE.lowConf = Number(document.getElementById('lowConf').value) || 0.5;
}

function boot() {
  document.getElementById('btnReload').addEventListener('click', function () {
    readControls();
    loadQueue();
    loadGold();
  });
  document.getElementById('btnExport').addEventListener('click', exportCsv);
  document.getElementById('btnGold').addEventListener('click', function () {
    var p = document.getElementById('goldPanel');
    p.hidden = !p.hidden;
    if (!p.hidden) { loadGold(); }
  });
  // 筛选条件改变即生效(群 id 需要失焦/回车,低置信线直接选)
  document.getElementById('chatId').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); readControls(); loadQueue(); loadGold(); }
  });
  document.getElementById('lowConf').addEventListener('change', function () {
    readControls();
    loadQueue();
  });

  loadRole();
  loadStandards();
  loadGold();
  loadQueue();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
