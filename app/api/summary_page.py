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
</style>
</head>
<body>
<div class="wrap">
  <h1>群聊总结</h1>
  <div class="sub">查看一次完整对话 · 主题标签 · LLM 总结 · 验证分类效果</div>

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

function reload(){loadConversations();}
window.addEventListener('DOMContentLoaded', loadConversations);
</script>
</body>
</html>
"""
