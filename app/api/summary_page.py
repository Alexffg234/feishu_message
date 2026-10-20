"""总结可视化页面(内建 HTML)。

浏览器访问 GET /summary 即可查看:
- 顶部切换对话
- 展示对话主题标签 + LLM 总结文案
- 展示完整对话的回复树(缩进表示层级)

纯前端 JS fetch 后端接口,无构建依赖。浅色主题。
"""

SUMMARY_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>群聊总结</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --border:#e3e6ea;
    --text:#1f2329; --muted:#6b7280; --primary:#185fa5; --accent:#0f6e56;
    --tag:#e6f1fb; --tagtext:#0c447c;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.6;padding:24px}
  .wrap{max-width:860px;margin:0 auto}
  h1{font-size:22px;font-weight:600;margin-bottom:4px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:20px}
  .bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:20px}
  .bar select{padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:#fff;font-size:14px;min-width:280px}
  .refresh{padding:8px 16px;border:none;border-radius:8px;background:var(--primary);color:#fff;cursor:pointer;font-size:14px}
  .refresh:hover{opacity:.9}
  .llm-status{font-size:12px;padding:4px 10px;border-radius:20px}
  .llm-on{background:#e1f5ee;color:#085041}
  .llm-off{background:#fcebeb;color:#791f1f}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}
  .topic{font-size:14px;display:inline-block;padding:4px 12px;background:var(--tag);color:var(--tagtext);border-radius:20px;margin-bottom:12px}
  .summary-title{font-size:15px;font-weight:600;margin-bottom:8px}
  .summary{font-size:14px;color:var(--text);white-space:pre-wrap;background:#fafbfc;border:1px solid var(--border);border-radius:8px;padding:12px}
  .empty-note{color:var(--muted);font-size:13px}
  .msg{margin:10px 0;padding:10px 12px;border-radius:8px;background:#fafbfc;border-left:3px solid var(--primary)}
  .msg .who{font-size:12px;color:var(--muted);margin-bottom:2px}
  .msg .body{font-size:14px;word-break:break-word}
  .msg .msg-img{max-width:320px;max-height:320px;border-radius:8px;border:1px solid #e4e7eb;cursor:zoom-in;display:block}
  .msg .msg-video{max-width:360px;max-height:320px;border-radius:8px;border:1px solid #e4e7eb;display:block;background:#000}
  .msg .media-name{font-size:12px;color:var(--muted);margin-top:4px;word-break:break-all}
  .msg .msg-file{display:inline-flex;align-items:center;gap:8px;max-width:100%;padding:8px 12px;border:1px solid var(--accent);border-radius:8px;background:#eaf3de;font-size:13px;word-break:break-all}
  .msg .msg-file .fname{color:var(--text);font-weight:500}
  .msg .msg-file a.dl{text-decoration:none;color:var(--primary);font-weight:500;white-space:nowrap}
  .msg .msg-file a.dl:hover{text-decoration:underline}
  .indent-0{margin-left:0}
  .indent-1{margin-left:24px;border-left-color:var(--accent)}
  .indent-2{margin-left:48px;border-left-color:#e2a03f}
  .indent-3{margin-left:72px;border-left-color:#9b5de5}
  .indent-4{margin-left:96px;border-left-color:#d4537e}
  .meta{font-size:12px;color:var(--muted);margin-top:12px}
  .loading{color:var(--muted);font-size:14px;padding:20px 0}
  .stats-overall{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .stat-chip{background:#fafbfc;border:1px solid var(--border);border-radius:8px;padding:8px 14px;font-size:13px;color:var(--muted);min-width:88px}
  .stat-chip b{color:var(--text);font-size:18px;display:block;line-height:1.3}
  .topic-row{border:1px solid var(--border);border-radius:10px;margin-bottom:8px;overflow:hidden}
  .topic-head{display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;background:#fff;flex-wrap:wrap}
  .topic-head:hover{background:#f8fafc}
  .topic-name{font-weight:600;font-size:14px}
  .topic-stat{font-size:12px;color:var(--muted)}
  .ai-badge{font-size:12px;padding:2px 10px;border-radius:20px;margin-left:auto}
  .ai-ready{background:#e1f5ee;color:#085041}
  .ai-stale{background:#fdf3e0;color:#7a4d05}
  .ai-missing{background:#eceef1;color:#6b7280}
  .topic-body{padding:12px 14px;border-top:1px solid var(--border);background:#fafbfc}
  .ai-block{margin-bottom:10px}
  .ai-block .k{font-size:13px;font-weight:600;color:var(--muted);margin-bottom:2px}
  .ai-block .v{font-size:14px;white-space:pre-wrap;word-break:break-word}
  .status-badge{font-size:12px;padding:2px 10px;border-radius:20px;background:#fdf3e0;color:#7a4d05;margin-left:6px}
  .status-done{background:#e1f5ee;color:#085041}
  .gen-meta{font-size:12px;color:var(--muted);margin-top:8px}
  .regen{padding:6px 14px;border:1px solid var(--border);border-radius:8px;background:#fff;cursor:pointer;font-size:13px;margin-top:6px}
  .regen:hover{border-color:var(--primary);color:var(--primary)}
</style>
</head>
<body>
<div class="wrap">
  <h1>群聊总结</h1>
  <div class="sub">问题分类统计 · 每类问题 AI 分析(问题/排查/解决) · 单对话查看 · 验证分类效果</div>

  <div class="card">
    <div class="summary-title">问题分类统计</div>
    <div id="statsBox" class="empty-note">加载中…</div>
  </div>

  <div class="bar">
    <select id="convSelect" onchange="loadSummary(this.value)"></select>
    <button class="refresh" onclick="reload()">刷新</button>
    <span id="llmStatus" class="llm-status"></span>
  </div>

  <div class="card">
    <div id="topicBox" class="topic" style="display:none"></div>
    <div class="summary-title">LLM 总结</div>
    <div id="summaryBox" class="empty-note">加载中…</div>
  </div>

  <div class="card">
    <div class="summary-title">完整对话</div>
    <div id="chatBox" class="empty-note">加载中…</div>
    <div id="meta" class="meta"></div>
  </div>
</div>

<script>
let convs = [];

function shortId(id){return id ? String(id).slice(0,18)+'…' : '';}

function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}

// 把文本里的 @占位符(如 @_user_1)替换成 @真实姓名;mentions 形如 {"@_user_1":"王国浩"}
function renderAt(text, mentions){
  text = text || '';
  if(mentions && typeof mentions === 'object'){
    for(const key in mentions){
      const name = mentions[key];
      if(name){ text = text.split(key).join('@'+name); }
    }
  }
  return text;
}

async function loadConversations(){
  const r = await fetch('/api/conversations?limit=50');
  convs = await r.json();
  const sel = document.getElementById('convSelect');
  sel.innerHTML = '';
  if(!convs.length){
    sel.innerHTML = '<option value="">暂无可展示的对话</option>';
    document.getElementById('summaryBox').textContent='暂无数据(可在飞书群里发几条并让机器人采集)';
    document.getElementById('chatBox').textContent='';
    return;
  }
  for(const c of convs){
    const label = (c.topic?('['+c.topic+'] '):'(未分类) ') + '#' + c.id + ' · ' + c.message_count + '条/' + c.participant_count + '人';
    const opt = document.createElement('option');
    opt.value = c.id; opt.textContent = label;
    sel.appendChild(opt);
  }
  sel.value = convs[0].id;
  loadSummary(sel.value, true);
}

async function loadSummary(id){
  if(!id) return;
  const boxS=document.getElementById('summaryBox');
  const boxC=document.getElementById('chatBox');
  const topicB=document.getElementById('topicBox');
  const meta=document.getElementById('meta');
  boxS.textContent='生成中…'; boxC.textContent='加载中…'; topicB.style.display='none'; meta.textContent='';
  try{
    const r=await fetch('/api/summary/'+id);
    if(!r.ok){boxS.textContent='对话不存在或加载失败';boxC.textContent='';return;}
    const d=await r.json();

    const st=document.getElementById('llmStatus');
    if(d.enabled){st.textContent='LLM 已启用';st.className='llm-status llm-on';}
    else{st.textContent='LLM 未配置';st.className='llm-status llm-off';}

    if(d.topic){topicB.textContent='主题 · '+d.topic;topicB.style.display='inline-block';}
    if(d.summary){boxS.className='summary';boxS.textContent=d.summary;}
    else{boxS.className='empty-note';boxS.textContent = d.enabled?'(生成失败,请稍后重试)':'(未配置 LLM,无法生成总结文案;在服务器 config/.env 填 LLM_API_BASE/KEY/MODEL 后生效)';}

    boxC.className='';
    boxC.innerHTML = renderTree(d.tree||[], 0) || '<span class="empty-note">该对话无消息内容</span>';
    meta.textContent = '共 '+d.message_count+' 条消息 · 会话ID #'+d.conversation_id;
  }catch(e){
    boxS.textContent='请求失败: '+e; boxC.textContent='';
  }
}

function renderTree(nodes, depth){
  let html='';
  for(const n of nodes){
    const m=n.message;
    if(m){
      const who = m.sender_name ? m.sender_name : (m.sender_id ? shortId(m.sender_id) : '?');
      let bodyHtml;
      if(m.message_type === 'media'){
        // 视频消息
        if(m.file_url){
          bodyHtml = '<video class="msg-video" src="'+esc(m.file_url)+'" controls preload="metadata"'
                   + (m.image_url ? ' poster="'+esc(m.image_url)+'"' : '') + '></video>'
                   + (m.file_name ? '<div class="media-name">'+esc(m.file_name)+'</div>' : '');
        } else if(m.image_url){
          bodyHtml = '<img class="msg-img" src="'+esc(m.image_url)+'" loading="lazy" alt="[视频封面]" onclick="window.open(this.src)">'
                   + (m.file_name ? '<div class="media-name">'+esc(m.file_name)+'</div>' : '');
        } else {
          bodyHtml = '<span class="empty-note">[视频(未落盘 '+(m.file_name||m.file_key||'')+')]</span>';
        }
      } else if(m.message_type === 'file'){
        // 普通文件消息:展示可下载的文件卡片
        if(m.file_url){
          bodyHtml = '<span class="msg-file"><span class="fname">'+esc(m.file_name || '文件')+'</span>'
                   + '<a class="dl" href="'+esc(m.file_url)+'" target="_blank" rel="noopener" download>下载</a></span>';
        } else {
          bodyHtml = '<span class="empty-note">[文件(未落盘 '+esc(m.file_name||m.file_key||'')+')]</span>';
        }
      } else if(m.image_url){
        // 图片消息:展示七牛图片
        bodyHtml = '<img class="msg-img" src="'+esc(m.image_url)+'" loading="lazy" alt="[图片]" onclick="window.open(this.src)">';
      } else if(m.message_type === 'image'){
        // 图片但未落盘(未配置七牛/下载失败):占位提示
        bodyHtml = '<span class="empty-note">[图片(未落盘 '+(m.image_key||'')+')]</span>';
      } else {
        bodyHtml = esc(renderAt(m.text, m.mentions) || '(' + (m.message_type||'非文本') + ')');
      }
      html += '<div class="msg indent-'+Math.min(depth,4)+'">'
            + '<div class="who">'+esc(who)+'</div>'
            + '<div class="body">'+bodyHtml+'</div></div>';
    }
    if(n.children && n.children.length){
      html += renderTree(n.children, depth+1);
    }
  }
  return html;
}

let topicsData = [];

function fmtMs(v){
  if(!v) return '';
  const d = new Date(Number(v));
  const p = n => String(n).padStart(2,'0');
  return (d.getMonth()+1)+'-'+d.getDate()+' '+p(d.getHours())+':'+p(d.getMinutes());
}

async function loadStats(){
  const box = document.getElementById('statsBox');
  box.className = 'empty-note';
  try{
    const r = await fetch('/api/topics?limit=50');
    const d = await r.json();
    topicsData = d.topics || [];
    const o = d.overall || {};
    let html = '<div class="stats-overall">'
      + '<div class="stat-chip"><b>'+(o.topic_count||0)+'</b>问题类别</div>'
      + '<div class="stat-chip"><b>'+(o.conversation_count||0)+'</b>对话(案例)</div>'
      + '<div class="stat-chip"><b>'+(o.message_count||0)+'</b>消息总数</div>'
      + '<div class="stat-chip"><b>'+(o.unclassified_conversations||0)+'</b>未分类对话</div>'
      + '</div>';
    if(!topicsData.length){
      html += '<div class="empty-note">暂无已分类的问题(消息入库并完成主题分类后出现)</div>';
    }
    topicsData.forEach((t,i)=>{
      const badge = t.summary_status==='ready'
        ? '<span class="ai-badge ai-ready">AI 已生成</span>'
        : t.summary_status==='stale'
        ? '<span class="ai-badge ai-stale">AI 待更新</span>'
        : '<span class="ai-badge ai-missing">AI 未生成</span>';
      html += '<div class="topic-row">'
        + '<div class="topic-head" onclick="toggleTopic('+i+')">'
        + '<span class="topic-name">'+esc(t.topic)+'</span>'
        + '<span class="topic-stat">'+t.conversation_count+' 案例</span>'
        + '<span class="topic-stat">'+t.message_count+' 条消息</span>'
        + '<span class="topic-stat">'+t.participant_count+' 人</span>'
        + '<span class="topic-stat">最近活跃 '+fmtMs(t.last_at)+'</span>'
        + badge
        + '</div>'
        + '<div class="topic-body" id="topicBody'+i+'" style="display:none"></div>'
        + '</div>';
    });
    box.innerHTML = html;
    box.className = '';
    if(topicsData.length) toggleTopic(0, true);
  }catch(e){
    box.textContent = '统计加载失败: ' + e;
  }
}

const topicLoaded = {};

async function toggleTopic(i, forceOpen){
  const body = document.getElementById('topicBody'+i);
  if(!body || !topicsData[i]) return;
  const willShow = body.style.display==='none' || forceOpen;
  body.style.display = willShow ? 'block' : 'none';
  if(!willShow || topicLoaded[i]) return;
  const t = topicsData[i];
  body.innerHTML = '<span class="empty-note">AI 分析加载中…</span>';
  try{
    const r = await fetch('/api/topics/summary?topic='+encodeURIComponent(t.topic));
    const d = await r.json();
    if(!r.ok){ body.innerHTML = '<span class="empty-note">'+(d.detail||'加载失败')+'</span>'; return; }
    topicLoaded[i] = true;
    if(!d.found){
      body.innerHTML = '<span class="empty-note">AI 分析尚未生成(后台定时任务会自动补齐,稍后刷新本页)</span>'
        + '<br><button class="regen" onclick="regenerateTopic('+i+')">立即生成</button>';
      return;
    }
    let html = '';
    if(d.stale) html += '<div class="gen-meta">该问题有新消息,AI 分析待后台自动更新(也可手动重新生成)</div>';
    const statusCls = d.status==='已解决' ? 'status-badge status-done' : 'status-badge';
    html += '<div class="ai-block"><div class="k">问题是什么 <span class="'+statusCls+'">'+esc(d.status||'未知')+'</span></div>'
      + '<div class="v">'+esc(d.problem||'(无)')+'</div></div>'
      + '<div class="ai-block"><div class="k">如何排查</div><div class="v">'+esc(d.investigation||'(无)')+'</div></div>'
      + '<div class="ai-block"><div class="k">如何解决</div><div class="v">'+esc(d.resolution||'(无)')+'</div></div>'
      + '<div class="gen-meta">生成于 '+(d.generated_at ? new Date(d.generated_at).toLocaleString() : '-')+'</div>'
      + '<button class="regen" onclick="regenerateTopic('+i+')">重新生成</button>';
    body.innerHTML = html;
  }catch(e){
    body.innerHTML = '<span class="empty-note">加载失败: '+e+'</span>';
  }
}

async function regenerateTopic(i){
  const t = topicsData[i];
  const body = document.getElementById('topicBody'+i);
  if(!t || !body) return;
  body.innerHTML = '<span class="empty-note">AI 重新生成中(约 5~15 秒)…</span>';
  try{
    const r = await fetch('/api/topics/summary?topic='+encodeURIComponent(t.topic), {method:'POST'});
    if(!r.ok){
      const d = await r.json().catch(()=>({}));
      body.innerHTML = '<span class="empty-note">生成失败: '+(d.detail||r.status)+'</span>';
      return;
    }
    topicLoaded[i] = false;
    await toggleTopic(i, true);
    loadStats();
  }catch(e){
    body.innerHTML = '<span class="empty-note">生成失败: '+e+'</span>';
  }
}

function reload(){loadConversations();loadStats();}
window.addEventListener('DOMContentLoaded', ()=>{ loadConversations(); loadStats(); });
</script>
</body>
</html>
"""
