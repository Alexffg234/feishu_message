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
    boxC.innerHTML = renderTimeline(d.tree||[]) || '<span class="empty-note">该对话无消息内容</span>';
    meta.textContent = '共 '+d.message_count+' 条消息 · 会话ID #'+d.conversation_id;
  }catch(e){
    boxS.textContent='请求失败: '+e; boxC.textContent='';
  }
}

function timeOf(m){
  const t = Number(m && m.create_time);
  return (isFinite(t) && t > 0) ? t : Number.MAX_SAFE_INTEGER;
}

// 把回复树拍平成消息列表,并按时间升序排序(完整对话按时间顺序展示)
function flattenTree(nodes){
  const out = [];
  const walk = ns => {
    for(const n of (ns||[])){
      if(n.message) out.push(n.message);
      if(n.children && n.children.length) walk(n.children);
    }
  };
  walk(nodes);
  out.sort((a,b) => timeOf(a) - timeOf(b));
  return out;
}

// 两个问题块之间是否需要横杠分隔:后端已对「带已有标签的新消息并入对话」的首条消息
// 打上 block_start=1,前端在该消息前画横杠
function isSep(m){
  return m.block_start === 1 || m.block_start === true;
}

// 单条消息的正文(文本/图片/视频/文件)
function msgBodyHtml(m){
  if(m.message_type === 'media'){
    // 视频消息
    if(m.file_url){
      return '<video class="msg-video" src="'+esc(m.file_url)+'" controls preload="metadata"'
           + (m.image_url ? ' poster="'+esc(m.image_url)+'"' : '') + '></video>'
           + (m.file_name ? '<div class="media-name">'+esc(m.file_name)+'</div>' : '');
    } else if(m.image_url){
      return '<img class="msg-img" src="'+esc(m.image_url)+'" loading="lazy" alt="[视频封面]" onclick="window.open(this.src)">'
           + (m.file_name ? '<div class="media-name">'+esc(m.file_name)+'</div>' : '');
    } else {
      return '<span class="empty-note">[视频(未落盘 '+(m.file_name||m.file_key||'')+')]</span>';
    }
  } else if(m.message_type === 'file'){
    // 普通文件消息:展示可下载的文件卡片
    if(m.file_url){
      return '<span class="msg-file"><span class="fname">'+esc(m.file_name || '文件')+'</span>'
           + '<a class="dl" href="'+esc(m.file_url)+'" target="_blank" rel="noopener" download>下载</a></span>';
    } else {
      return '<span class="empty-note">[文件(未落盘 '+esc(m.file_name||m.file_key||'')+')]</span>';
    }
  } else if(m.image_url){
    // 图片消息:展示七牛图片
    return '<img class="msg-img" src="'+esc(m.image_url)+'" loading="lazy" alt="[图片]" onclick="window.open(this.src)">';
  } else if(m.message_type === 'image'){
    // 图片但未落盘(未配置七牛/下载失败):占位提示
    return '<span class="empty-note">[图片(未落盘 '+(m.image_key||'')+')]</span>';
  } else {
    return esc(renderAt(m.text, m.mentions) || '(' + (m.message_type||'非文本') + ')');
  }
}

// 完整对话:按时间顺序平铺,新问题块(block_start=1)前用横杠分隔
function renderTimeline(tree){
  const msgs = flattenTree(tree);
  if(!msgs.length) return '';
  let html = '';
  for(let i=0; i<msgs.length; i++){
    const m = msgs[i];
    if(i > 0 && isSep(m)){
      html += '<div class="chat-sep"><span>'+esc(fmtMs(m.create_time))+'</span></div>';
    }
    const who = m.sender_name ? m.sender_name : (m.sender_id ? shortId(m.sender_id) : '?');
    html += '<div class="msg">'
          + '<div class="who">'+esc(who)+'<span class="mtime">'+esc(fmtMs(m.create_time))+'</span></div>'
          + '<div class="body">'+msgBodyHtml(m)+'</div></div>';
  }
  return html;
}

let topicsData = [];
let groupData = [];

// 单个 topic 行(分组内嵌套时 nested=true)
function topicRowHtml(i, t, nested){
  const badge = t.summary_status==='ready'
    ? '<span class="ai-badge ai-ready">AI 已生成</span>'
    : t.summary_status==='stale'
    ? '<span class="ai-badge ai-stale">AI 待更新</span>'
    : '<span class="ai-badge ai-missing">AI 未生成</span>';
  return '<div class="topic-row'+(nested?' sub-row':'')+'">'
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
}

function toggleGroup(gi){
  const body = document.getElementById('groupBody'+gi);
  if(!body) return;
  body.style.display = body.style.display==='none' ? 'block' : 'none';
}

function toggleSubGroup(gi, si){
  const body = document.getElementById('subGroupBody'+gi+'_'+si);
  if(!body) return;
  body.style.display = body.style.display==='none' ? 'block' : 'none';
}

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
    const [r, g] = await Promise.all([
      fetch('/api/topics?limit=50'),
      fetch('/api/topic_groups'),
    ]);
    const d = await r.json();
    const gd = await g.json();
    topicsData = d.topics || [];
    groupData = gd.groups || [];
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
    // 大类(分组):支持两级。parent 非空的组是指向组的子类(组内再嵌 topic);
    // parent 指向的组不存在时按顶级兜底;组内小类按分组配置顺序展示
    const groupOf = {};
    groupData.forEach((gp, gi) => { (gp.topics||[]).forEach(t => { groupOf[t] = gi; }); });
    const groupNames = new Set(groupData.map(gp => gp.name));
    const isTop = gp => !gp.parent || !groupNames.has(gp.parent);
    let firstGroup = -1;
    groupData.forEach((gp, gi) => {
      if(!isTop(gp)) return;
      const direct = (gp.topics||[]).map(n => topicsData.find(t => t.topic === n)).filter(Boolean);
      let subInner = '';
      const subTopics = [];
      let subCount = 0;
      groupData.forEach((s, si) => {
        if(s.parent !== gp.name) return;
        const ssubs = (s.topics||[]).map(n => topicsData.find(t => t.topic === n)).filter(Boolean);
        if(!ssubs.length) return;
        subCount++;
        subTopics.push(...ssubs);
        const sconv = ssubs.reduce((a,t)=>a+(t.conversation_count||0),0);
        const smsg = ssubs.reduce((a,t)=>a+(t.message_count||0),0);
        let sinner = '';
        for(const t of ssubs) sinner += topicRowHtml(topicsData.indexOf(t), t, true);
        subInner += '<div class="topic-row subgroup-row">'
          + '<div class="topic-head subgroup-head" onclick="toggleSubGroup('+gi+','+si+')">'
          + '<span class="topic-name">'+esc(s.name)+'</span>'
          + '<span class="topic-stat">'+ssubs.length+' 个小类</span>'
          + '<span class="topic-stat">'+sconv+' 案例</span>'
          + '<span class="topic-stat">'+smsg+' 条消息</span>'
          + '</div>'
          + '<div class="group-body subgroup-body" id="subGroupBody'+gi+'_'+si+'" style="display:none">'+sinner+'</div>'
          + '</div>';
      });
      const allTopics = [...subTopics, ...direct];
      if(!allTopics.length) return;
      if(firstGroup < 0) firstGroup = gi;
      const convSum = allTopics.reduce((a,t)=>a+(t.conversation_count||0),0);
      const msgSum = allTopics.reduce((a,t)=>a+(t.message_count||0),0);
      let inner = subInner;
      for(const t of direct) inner += topicRowHtml(topicsData.indexOf(t), t, true);
      const countLabel = subCount
        ? subCount+' 个子类'+(direct.length?' / '+direct.length+' 个小类':'')
        : direct.length+' 个小类';
      html += '<div class="topic-row group-row">'
        + '<div class="topic-head group-head" onclick="toggleGroup('+gi+')">'
        + '<span class="topic-name">'+esc(gp.name)+'</span>'
        + '<span class="topic-stat">'+countLabel+'</span>'
        + '<span class="topic-stat">'+convSum+' 案例</span>'
        + '<span class="topic-stat">'+msgSum+' 条消息</span>'
        + '</div>'
        + '<div class="group-body" id="groupBody'+gi+'" style="display:none">'+inner+'</div>'
        + '</div>';
    });
    // 未分组 topic 平铺
    for(const t of topicsData){
      if(groupOf[t.topic] !== undefined) continue;
      html += topicRowHtml(topicsData.indexOf(t), t, false);
    }
    box.innerHTML = html;
    box.className = '';
    if(firstGroup >= 0) toggleGroup(firstGroup);
    else if(topicsData.length) toggleTopic(0, true);
  }catch(e){
    box.textContent = '统计加载失败: ' + e;
  }
}

// 渲染媒体画廊(图片/视频/文件缩略图,带 #N 编号,与文案引用一致)
function mediaGalleryHtml(items){
  if(!items || !items.length) return '';
  let g = '<div class="media-gallery">';
  for(const mm of items){
    if(mm.type === 'image'){
      g += '<span class="media-item"><img class="media-thumb" src="'+esc(mm.url)+'" loading="lazy" alt="图#'+mm.idx+'" onclick="window.open(this.src)"><span class="media-cap">图#'+mm.idx+'</span></span>';
    } else if(mm.type === 'media'){
      g += '<span class="media-item"><video class="media-thumb" src="'+esc(mm.url)+'" controls preload="metadata"></video><span class="media-cap">视频#'+mm.idx+'</span></span>';
    } else {
      g += '<span class="media-item"><a class="media-filelink" href="'+esc(mm.url)+'" target="_blank" rel="noopener">文件#'+mm.idx+': '+(esc(mm.name)||'下载')+'</a></span>';
    }
  }
  return g + '</div>';
}

// 从文案里提取引用的 #N 编号,按出现顺序取出对应媒体
function pickMediaByRef(text, media){
  if(!text || !media || !media.length) return [];
  const seen = [];
  const re = /#(\d+)/g;
  let m;
  while((m = re.exec(text)) !== null){
    const item = media.find(x => x.idx === Number(m[1]));
    if(item && !seen.includes(item)) seen.push(item);
  }
  return seen;
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
      + '<div class="ai-block"><div class="k">如何排查</div><div class="v">'+esc(d.investigation||'(无)')+'</div>'
      + mediaGalleryHtml(pickMediaByRef(d.investigation, d.media)) + '</div>'
      + '<div class="ai-block"><div class="k">如何解决</div><div class="v">'+esc(d.resolution||'(无)')+'</div>'
      + mediaGalleryHtml(pickMediaByRef(d.resolution, d.media)) + '</div>';
    html += '<div class="gen-meta">生成于 '+(d.generated_at ? new Date(d.generated_at).toLocaleString() : '-')+'</div>'
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

let statsLoaded = false;

function switchTab(n){
  document.getElementById('tab1').style.display = n === 1 ? 'block' : 'none';
  document.getElementById('tab2').style.display = n === 2 ? 'block' : 'none';
  document.getElementById('tabBtn1').className = 'tab' + (n === 1 ? ' active' : '');
  document.getElementById('tabBtn2').className = 'tab' + (n === 2 ? ' active' : '');
  if(n === 2 && !statsLoaded){
    statsLoaded = true;
    loadStats();
  }
}

window.addEventListener('DOMContentLoaded', loadConversations);
