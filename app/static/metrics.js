/* ==========================================================================
   标签指标页
   - 数据源: GET /api/topic_metrics(只读)
   - 本页的职责不是「显示数字」,而是把数字翻译成「现在该做什么」:
     每个指标都过一遍阈值,汇成顶部诊断横幅;卡片只是给横幅提供证据。
   - 阈值集中在 TH 里,改口径只改这一处(页面底部说明也是按它写的)
   ========================================================================== */
(function () {
  'use strict';

  var TH = {
    unclassifiedWarn: 0.15,   // 未分类率起警
    unclassifiedDanger: 0.30, // 未分类率转红
    fallbackWarn: 0.20,       // 召回漏检率起警
    unusedRatioWarn: 0.40,    // 标准标签里「从没被用过」的占比起警
    mergeRateWarn: 0.70       // 归并率低于此值 = 碎片还散着
  };

  var REFRESH_MS = 30000;
  var timer = null;
  var lastData = null;

  /* ---------------------------------------------------------------- 工具 */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function pct(x, digits) {
    var v = Number(x);
    if (!isFinite(v)) { return '—'; }
    return (v * 100).toFixed(digits == null ? 1 : digits) + '%';
  }

  function num(x) {
    var v = Number(x);
    return isFinite(v) ? String(v) : '—';
  }

  function stamp() {
    var d = new Date();
    var p = function (n) { return ('0' + n).slice(-2); };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* ------------------------------------------------------- 横条行(通用) */
  function barRow(label, ratio, rightText, tone) {
    var row = el('div', 'mt-row');
    row.appendChild(el('span', 'mt-row__lbl', label));

    var bar = el('div', 'mt-bar' + (tone ? ' mt-bar--' + tone : ''));
    var fill = el('i');
    var w = Math.max(0, Math.min(1, Number(ratio) || 0)) * 100;
    fill.style.width = w.toFixed(2) + '%';
    bar.appendChild(fill);
    row.appendChild(bar);

    var right = el('span', 'mt-row__num');
    right.innerHTML = rightText == null ? '' : String(rightText);
    row.appendChild(right);
    return row;
  }

  function miniBox(title, valueHtml, detailHtml, warn) {
    var box = el('div', 'mt-mini__box' + (warn ? ' mt-mini__box--warn' : ''));
    box.appendChild(el('div', 'mt-mini__t', title));
    var v = el('div', 'mt-mini__n');
    v.innerHTML = valueHtml;
    box.appendChild(v);
    if (detailHtml) {
      var d = el('div', 'mt-mini__d');
      d.innerHTML = detailHtml;
      box.appendChild(d);
    }
    return box;
  }

  /* --------------------------------------------------------- 诊断横幅 */
  function verdicts(m) {
    var out = [];
    var un = m.unclassified || {};
    var sc = m.standard_coverage || {};
    var rv = m.review || {};
    var rc = m.recall || {};

    // 1. 没有 gold set:所有「准确率」都没有基准。这条要放在最前面,它会改变
    //    你对下面所有数字的解读方式。
    if (!rv.gold_groups) {
      out.push({
        level: 'danger',
        text: '还没有任何人工确认的组(gold set = 0)。',
        action: '没有基准,下面的准确率类指标都只是模型自说自话。先去 <code>/review</code> 定标二三十组。'
      });
    }

    // 2. 未分类率:闭集判别的代价就体现在这里
    var rate = Number(un.rate) || 0;
    if (un.total_groups && rate >= TH.unclassifiedDanger) {
      out.push({
        level: 'danger',
        text: '未分类率 ' + pct(rate) + '(' + num(un.groups) + '/' + num(un.total_groups) + ' 组),已经过 ' + pct(TH.unclassifiedDanger, 0) + '。',
        action: '闭集缺现象,不是模型不行。用 <code>scripts/unclassified.py --words</code> 看这批组在说什么、缺哪个大类。'
      });
    } else if (un.total_groups && rate >= TH.unclassifiedWarn) {
      out.push({
        level: 'warn',
        text: '未分类率 ' + pct(rate) + '(' + num(un.groups) + ' 组),超过 ' + pct(TH.unclassifiedWarn, 0) + ' 的警戒线。',
        action: '先看是不是别名表漏了 near-miss:跑 <code>scripts/suggest_aliases.py</code>,达标再 <code>--apply</code>。'
      });
    }

    // 3. 非标准标签漏网 = 别名没收敛住
    var offN = Number(sc.off_standard_count) || 0;
    if (offN > 0) {
      out.push({
        level: 'warn',
        text: '库里正在使用 ' + offN + ' 个非标准标签(不在标准集里)。',
        action: '这些是别名表的漏洞,以后还会继续产生。跑 <code>scripts/suggest_aliases.py</code> 生成映射。'
      });
    }

    // 4. 标准标签长期没人用 = 标准集冗余
    var stdN = Number(sc.standard_count) || 0;
    var unusedN = stdN - (Number(sc.used_standard_count) || 0);
    if (stdN && unusedN / stdN >= TH.unusedRatioWarn) {
      out.push({
        level: 'info',
        text: '标准标签里有 ' + unusedN + '/' + stdN + ' 个从没被用过。',
        action: '要么标准集有冗余(可以删),要么提示词没覆盖到这些现象(要补别名/样例)。瘦身之后这个数应该明显下降。'
      });
    }

    // 5. 召回漏检率:窄化后仍判 UNKNOWN 的比例
    if (Number(rc.narrowed) > 0 && Number(rc.fallback_rate) >= TH.fallbackWarn) {
      out.push({
        level: 'warn',
        text: '召回漏检率 ' + pct(rc.fallback_rate) + '(窄化后仍判 UNKNOWN 的比例)。',
        action: '说明候选集经常没含正确答案。调大 <code>TOPIC_RECALL_TOP_K</code>,或补历史样例让打分更准。'
      });
    }

    if (!out.length) {
      out.push({
        level: 'ok',
        text: '指标都在阈值内:未分类率 ' + pct(rate) + ',标准标签覆盖 ' + pct(sc.rate) + ',gold set ' + num(rv.gold_groups) + ' 组。',
        action: '保持现在的复核节奏即可 —— gold set 越大,这里的数才越可信。'
      });
    }
    return out;
  }

  function renderVerdict(m) {
    var root = document.getElementById('verdict');
    clear(root);
    var wrap = el('div', 'mt-verdict');
    var mark = { ok: '✓', warn: '!', danger: '×', info: 'i' };
    verdicts(m).forEach(function (v) {
      var box = el('div', 'mt-v mt-v--' + v.level);
      box.appendChild(el('span', 'mt-v__ico', mark[v.level] || 'i'));
      var txt = el('div', 'mt-v__txt');
      txt.appendChild(el('span', null, v.text));
      var act = el('span', 'mt-v__act');
      act.innerHTML = v.action || '';
      txt.appendChild(act);
      box.appendChild(txt);
      wrap.appendChild(box);
    });
    root.appendChild(wrap);
  }

  /* ------------------------------------------------------------- KPI */
  function kpi(valueHtml, label, hint, tone) {
    var box = el('div', 'mt-kpi' + (tone ? ' mt-kpi--' + tone : ''));
    var v = el('div', 'mt-kpi__num');
    v.innerHTML = valueHtml;
    box.appendChild(v);
    box.appendChild(el('div', 'mt-kpi__lbl', label));
    if (hint) { box.appendChild(el('div', 'mt-kpi__hint', hint)); }
    return box;
  }

  function renderKpis(m) {
    var root = document.getElementById('kpis');
    clear(root);
    var un = m.unclassified || {};
    var sc = m.standard_coverage || {};
    var rv = m.review || {};

    var unRate = Number(un.rate) || 0;
    var unTone = unRate >= TH.unclassifiedDanger ? 'danger'
      : (unRate >= TH.unclassifiedWarn ? 'warn' : '');

    root.appendChild(kpi(
      pct(unRate),
      '未分类率',
      num(un.groups) + ' / ' + num(un.total_groups) + ' 组',
      unTone
    ));
    root.appendChild(kpi(
      pct(sc.rate),
      '标准标签覆盖率',
      num(sc.used_standard_count) + ' / ' + num(sc.standard_count) + ' 个被用到'
    ));
    root.appendChild(kpi(
      num(sc.off_standard_count),
      '非标准标签漏网',
      sc.off_standard_count ? '用了却不在册' : '全部收敛在标准集内',
      sc.off_standard_count ? 'warn' : ''
    ));
    root.appendChild(kpi(
      num(rv.pending_groups),
      '待复核组',
      rv.pending_groups ? '未分类优先' : '队列已清空'
    ));
    root.appendChild(kpi(
      num(rv.gold_groups),
      'gold set 规模',
      rv.gold_groups ? '人工确认过 = 唯一基准' : '为 0 时准确率不作数',
      rv.gold_groups ? '' : 'danger'
    ));
    root.appendChild(kpi(
      num(m.topic_count),
      '库内标签总数',
      '低频占 ' + pct(m.low_frequency_ratio)
    ));
  }

  /* --------------------------------------------------- 置信度分布 */
  function renderConf(conf) {
    var root = document.getElementById('conf');
    clear(root);
    var c = conf || {};
    var total = (c.high || 0) + (c.medium || 0) + (c.low || 0) + (c.none || 0);
    if (!total) {
      root.appendChild(el('div', 'empty', '还没有已归类的组 — 先跑一轮分类'));
      return;
    }
    var rows = el('div', 'mt-rows');
    rows.appendChild(barRow('高 ≥0.8', (c.high || 0) / total,
      '<b>' + num(c.high) + '</b> 组 · ' + pct((c.high || 0) / total), 'ok'));
    rows.appendChild(barRow('中 0.5~0.8', (c.medium || 0) / total,
      '<b>' + num(c.medium) + '</b> 组 · ' + pct((c.medium || 0) / total), 'warn'));
    rows.appendChild(barRow('低 <0.5', (c.low || 0) / total,
      '<b>' + num(c.low) + '</b> 组 · ' + pct((c.low || 0) / total), 'danger'));
    rows.appendChild(barRow('无(旧格式)', (c.none || 0) / total,
      '<b>' + num(c.none) + '</b> 组 · ' + pct((c.none || 0) / total), 'faint'));
    root.appendChild(rows);
  }

  /* ------------------------------------------------- 标准标签用量 */
  function renderUsage(sc) {
    var root = document.getElementById('usage');
    var meta = document.getElementById('usageMeta');
    clear(root);

    var labels = (sc && sc.labels) || [];
    if (!labels.length) {
      root.appendChild(el('div', 'empty', '还没有配置标准标签(先跑 init_db.py 或 slim_topics.py --apply)'));
      meta.textContent = '';
      return;
    }

    var used = labels.filter(function (x) { return x.groups > 0; }).length;
    meta.textContent = used + '/' + labels.length + ' 个被用到';

    // 按大类归并;labels 已按用量倒序,所以大类按「最热标签」的顺序出现,
    // 主力大类自然排在前面。
    var order = [];
    var buckets = {};
    labels.forEach(function (x) {
      var g = x.group || '未分类组';
      if (!buckets[g]) { buckets[g] = []; order.push(g); }
      buckets[g].push(x);
    });

    order.forEach(function (g) {
      var block = el('div', 'mt-grp');
      var items = buckets[g];
      var zero = items.filter(function (x) { return !x.groups; }).length;
      var title = g + '(' + items.length + ' 个标签';
      if (zero) { title += ',其中 ' + zero + ' 个没用过'; }
      title += ')';
      block.appendChild(el('div', 'mt-grp__t', title));

      var chips = el('div', 'mt-chips');
      items.forEach(function (x) {
        var chip = el('span', 'mt-chip' + (x.groups ? '' : ' mt-chip--zero'));
        chip.appendChild(el('span', null, x.label));
        chip.appendChild(el('span', 'mt-chip__n', x.groups ? x.groups + ' 组' : '未用过'));
        chip.title = x.groups
          ? x.label + ':用于 ' + x.groups + ' 个组'
          : x.label + ':从没被用过 —— 标准集冗余,或提示词没覆盖到这种现象';
        chips.appendChild(chip);
      });
      block.appendChild(chips);
      root.appendChild(block);
    });
  }

  /* ----------------------------------------------- 非标准标签漏网 */
  function renderOffStd(sc) {
    var root = document.getElementById('offStd');
    var meta = document.getElementById('offMeta');
    clear(root);

    var off = (sc && sc.off_standard) || [];
    var total = Number((sc && sc.off_standard_count) || 0);
    meta.textContent = total ? total + ' 个' : '0 个';

    if (!off.length) {
      root.appendChild(el('div', 'mt-okline', '✓ 没有漏网标签 — 库里的标签全部收敛在标准集内'));
      return;
    }

    var chips = el('div', 'mt-chips');
    off.forEach(function (x) {
      var chip = el('span', 'mt-chip mt-chip--off');
      chip.appendChild(el('span', null, x.label));
      chip.appendChild(el('span', 'mt-chip__n', x.groups + ' 组'));
      chip.title = x.label + ':用了 ' + x.groups + ' 个组,但不在标准集里';
      chips.appendChild(chip);
    });
    root.appendChild(chips);

    if (total > off.length) {
      root.appendChild(el('div', 'mt-okline',
        '(只显示用量最多的 ' + off.length + ' 个,共 ' + total + ' 个)'));
    }
  }

  /* ------------------------------------------------- 归并与碎片 */
  function renderMerge(m) {
    var root = document.getElementById('merge');
    clear(root);
    var mergeRate = Number(m.merge_rate) || 0;
    var rows = el('div', 'mt-rows');
    rows.appendChild(barRow(
      '归并率', mergeRate,
      '<b>' + pct(mergeRate) + '</b>',
      mergeRate >= TH.mergeRateWarn ? 'ok' : 'warn'
    ));
    rows.appendChild(barRow(
      '低频标签占比', Number(m.low_frequency_ratio) || 0,
      '<b>' + pct(m.low_frequency_ratio) + '</b>',
      (Number(m.low_frequency_ratio) || 0) <= 0.3 ? 'ok' : 'warn'
    ));
    root.appendChild(rows);

    var mini = el('div', 'mt-mini');
    mini.style.marginTop = '12px';
    mini.appendChild(miniBox(
      '待归并碎片主题',
      num(m.remaining_fragmented_topics) + '<small> 个</small>',
      '同一标签还散在多个组里',
      Number(m.remaining_fragmented_topics) > 0
    ));
    mini.appendChild(miniBox(
      '低频标签(仅 1 组)',
      num(m.low_frequency_topic_count) + '<small> / ' + num(m.topic_count) + '</small>',
      '同义词分裂的直接度量,越少越好',
      (Number(m.low_frequency_ratio) || 0) > 0.3
    ));
    root.appendChild(mini);
  }

  /* ----------------------------------------------- 进程内指标 */
  function renderProcs(m) {
    var root = document.getElementById('procs');
    clear(root);
    var n = m.normalization || {};
    var rc = m.recall || {};
    var st = m.stitching || {};

    root.appendChild(miniBox(
      '归一化收敛率',
      n.total ? pct(n.rate) : '—<small> 还没调用</small>',
      n.total
        ? '别名 ' + num(n.alias_hit) + ' / 相似度 ' + num(n.similarity_hit) +
          ' / 直接命中 ' + num(n.standard_hit) + ',共 ' + num(n.total) + ' 次'
        : '本次进程内还没发生过标签归一化'
    ));

    root.appendChild(miniBox(
      '召回窄化率',
      rc.calls ? pct(rc.narrow_rate) : '—<small> 还没调用</small>',
      rc.calls
        ? num(rc.calls) + ' 次调用 · 漏检率 <b>' + pct(rc.fallback_rate) + '</b>' +
          '(' + num(rc.fallback) + '/' + num(rc.narrowed) + ')'
        : '本次进程内还没发生过候选召回',
      Number(rc.fallback_rate) >= TH.fallbackWarn && Number(rc.narrowed) > 0
    ));

    root.appendChild(miniBox(
      '缝合确认率',
      (Number(st.case_key) + Number(st.llm)) > 0
        ? pct(m.stitch_confirm_rate)
        : '—<small> 还没调用</small>',
      (Number(st.case_key) + Number(st.llm)) > 0
        ? '确定性锚点 ' + num(st.case_key) + ' / LLM 判定 ' + num(st.llm)
        : '本次进程内还没发生过会话缝合'
    ));
  }

  /* ------------------------------------------------------- 原始 JSON */
  function toggleJson() {
    var exist = document.getElementById('rawJson');
    if (exist) { exist.parentNode.removeChild(exist); return; }
    if (!lastData) { return; }
    var box = el('div', 'card');
    box.id = 'rawJson';
    var pre = el('pre');
    pre.style.cssText = 'font-size:12px;line-height:1.6;overflow:auto;max-height:60vh;'
      + 'color:var(--text-muted);white-space:pre-wrap;word-break:break-all;';
    pre.textContent = JSON.stringify(lastData, null, 2);
    box.appendChild(pre);
    var wrap = document.querySelector('.wrap');
    wrap.appendChild(box);
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ------------------------------------------------------------- 加载 */
  function fail(msg, detail) {
    var root = document.getElementById('verdict');
    clear(root);
    var wrap = el('div', 'mt-verdict');
    var box = el('div', 'mt-v mt-v--danger');
    box.appendChild(el('span', 'mt-v__ico', '×'));
    var txt = el('div', 'mt-v__txt');
    txt.appendChild(el('span', null, msg));
    var act = el('span', 'mt-v__act');
    act.innerHTML = detail || '';
    txt.appendChild(act);
    box.appendChild(txt);
    wrap.appendChild(box);
    root.appendChild(wrap);
  }

  function load() {
    var btn = document.getElementById('btnReload');
    btn.disabled = true;
    fetch('/api/topic_metrics', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) {
          throw new Error('HTTP ' + r.status);
        }
        return r.json();
      })
      .then(function (m) {
        lastData = m;
        renderVerdict(m);
        renderKpis(m);
        renderConf(m.confidence);
        renderUsage(m.standard_coverage);
        renderOffStd(m.standard_coverage);
        renderMerge(m);
        renderProcs(m);
        document.getElementById('stamp').textContent = '更新于 ' + stamp();
      })
      .catch(function (e) {
        // 500 多半不是前端问题:这个端点会读 messages.topic_conf / stage,
        // 库缺列时直接炸。把最可能的原因写出来,省得对着 Network 面板猜。
        fail(
          '指标加载失败:' + (e && e.message ? e.message : '未知错误'),
          '返回 500 时,多半是库缺 <code>topic_conf</code> / <code>stage</code> 列'
          + '(代码没部署或没重启,迁移在 <code>init_db()</code> 里跑)。'
          + '部署重启后再试;若是 401,请重新登录。'
        );
      })
      .then(function () {
        btn.disabled = false;
      });
  }

  /* --------------------------------------------------- 自动刷新 */
  function setAuto(on) {
    if (timer) { clearInterval(timer); timer = null; }
    if (!on) { return; }
    timer = setInterval(function () {
      // 页面在后台时不刷,回来时补一次(否则切回来看到的是旧数,还以为卡住)
      if (document.hidden) { return; }
      load();
    }, REFRESH_MS);
  }

  function boot() {
    document.getElementById('btnReload').addEventListener('click', load);
    document.getElementById('btnJson').addEventListener('click', toggleJson);
    document.getElementById('autoRefresh').addEventListener('change', function (e) {
      setAuto(e.target.checked);
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && timer) { load(); }
    });
    load();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
