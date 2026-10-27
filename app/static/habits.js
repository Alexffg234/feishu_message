let DATA = null;

function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}

// ---------- 简单 SVG 工具 ----------
function barChart(container, items, opts){
  opts = opts||{};
  const W=380,H=220,padL=64,padB=24,padT=10,padR=8;
  const cw=W-padL-padR,ch=H-padT-padB;
  if(!items||!items.length){container.innerHTML='<div class="empty-note">无数据</div>';return;}
  const max=Math.max(...items.map(i=>i.val),1);
  const colors=opts.colors||[];
  let svg='<svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'">';
  // 网格线
  for(let g=0;g<=4;g++){
    const y=padT+(ch*g/4);
    svg+='<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="#eef1f4" stroke-width="1"/>';
  }
  if(opts.horizontal){
    // 横向条形
    const bh=Math.min(18,(ch-8)/items.length);
    items.forEach((it,i)=>{
      const y=padT+8+i*bh;
      const w=Math.max(2,(it.val/max)*(cw-50));
      const fill=colors[i%colors.length]||opts.fill||'#185fa5';
      svg+='<text x="'+(padL-8)+'" y="'+(y+bh/2+4)+'" text-anchor="end" font-size="11" fill="#6b7280">'+esc(it.label)+'</text>';
      svg+='<rect x="'+padL+'" y="'+y+'" width="'+w+'" height="'+(bh-6)+'" rx="3" fill="'+fill+'"/>';
      svg+='<text x="'+(padL+w+4)+'" y="'+(y+bh/2+3)+'" font-size="10" fill="#1f2329">'+it.val+(opts.suffix||'')+'</text>';
    });
  } else {
    // 纵向柱
    const bw=Math.min(26,(cw-10)/items.length);
    items.forEach((it,i)=>{
      const x=padL+6+i*(bw+2);
      const h=Math.max(2,(it.val/max)*ch);
      const y=padT+(ch-h);
      const fill=colors[i%colors.length]||opts.fill||'#185fa5';
      svg+='<rect x="'+x+'" y="'+y+'" width="'+bw+'" height="'+h+'" rx="3" fill="'+fill+'"/>';
      svg+='<text x="'+(x+bw/2)+'" y="'+(y-4)+'" text-anchor="middle" font-size="10" fill="#1f2329">'+it.val+'</text>';
      svg+='<text x="'+(x+bw/2)+'" y="'+(H-6)+'" text-anchor="middle" font-size="9" fill="#6b7280">'+esc(it.label)+'</text>';
    });
  }
  svg+='</svg>';
  container.innerHTML=svg+(opts.note?'<div class="tip">'+opts.note+'</div>':'');
}

function donut(container, items){
  const W=200,H=200,cx=W/2,cy=H/2,R=78,r=48;
  if(!items||!items.length){container.innerHTML='<div class="empty-note">无数据</div>';return;}
  const total=items.reduce((a,i)=>a+i.val,0)||1;
  let angle=-Math.PI/2;
  const colors=['#185fa5','#2a9d8f','#e9c46a','#e76f51','#9b5de5','#f4a261','#457b9d','#8ecae6'];
  let arcs='';
  const parts=[];
  items.forEach((it,i)=>{
    const frac=it.val/total;
    const a2=angle+frac*Math.PI*2;
    const large=(a2-angle)>Math.PI?1:0;
    const x1=cx+R*Math.cos(angle), y1=cy+R*Math.sin(angle);
    const x2=cx+R*Math.cos(a2), y2=cy+R*Math.sin(a2);
    arcs+='<path d="M'+x1+' '+y1+' A'+R+' '+R+' 0 '+large+' 1 '+x2+' '+y2+'" fill="'+(colors[i%colors.length])+'" stroke="#fff" stroke-width="2"/>';
    parts.push({label:it.label, val:it.val, frac:frac, color:colors[i%colors.length]});
    angle=a2;
  });
  let svg='<svg width="'+(W+110)+'" height="'+H+'" viewBox="0 0 '+(W+110)+' '+H+'" style="display:block;margin:0 auto">';
  // 中心洞
  svg+='<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="#fff"/>';
  svg+='<text x="'+cx+'" y="'+(cy-4)+'" text-anchor="middle" font-size="20" font-weight="700" fill="#185fa5">'+total+'</text>';
  svg+='<text x="'+cx+'" y="'+(cy+14)+'" text-anchor="middle" font-size="10" fill="#6b7280">条消息</text>';
  svg+='<g>'+arcs+'</g>';
  // 图例（右侧）
  let ly=20;
  parts.forEach((p,i)=>{
    svg+='<rect x="'+(W+8)+'" y="'+(ly-8)+'" width="10" height="10" rx="2" fill="'+p.color+'"/>';
    svg+='<text x="'+(W+24)+'" y="'+ly+'" font-size="11" fill="#1f2329">'+esc(p.label)+'</text>';
    svg+='<text x="'+(W+90)+'" y="'+ly+'" text-anchor="end" font-size="11" fill="#6b7280">'+p.val+' ('+Math.round(p.frac*100)+'%)</text>';
    ly+=20;
  });
  svg+='</svg>';
  container.innerHTML=svg;
}

function showSenders(){
  const d=DATA||{};
  const items=(d.发言者TOP||[]).map(p=>({label:p.姓名, val:p.消息数}));
  barChart(document.getElementById('senders'), items, {horizontal:true, colors:['#185fa5','#2a9d8f','#e9c46a','#e76f51','#9b5de5','#f4a261','#457b9d'], suffix:' 条'});
}

function showTypes(){
  const d=DATA||{};
  const items=Object.entries(d.类型分布||{}).map(([k,v])=>({label:k, val:v}));
  donut(document.getElementById('types'), items);
}

function showLengths(){
  const d=DATA||{};
  const dist=d.文字长度分布||{};
  const items=Object.entries(dist).map(([k,v])=>({label:k, val:v.条数}));
  barChart(document.getElementById('lengths'), items, {horizontal:true, colors:['#2a9d8f','#e9c46a','#e76f51','#9b5de5'],suffix:' 条'});
}

function showHours(){
  const d=DATA||{};
  const items=(d.小时分布||[]).map(h=>({label:(h.hour<10?'0'+h.hour:h.hour)+'时', val:h.count}));
  barChart(document.getElementById('hours'), items, {horizontal:false, fill:'#185fa5'});
}

function showWeeks(){
  const d=DATA||{};
  const items=(d.周分布||[]).filter(w=>w.count>0).map(w=>({label:w.day, val:w.count}));
  barChart(document.getElementById('weeks'), items, {horizontal:false, colors:['#2a9d8f','#e9c46a','#e76f51','#9b5de5','#185fa5','#f4a261','#457b9d']});
}

function showInteraction(){
  const d=DATA||{};
  const el=document.getElementById('interact');
  const reply=d["回复率%"]||0;
  el.innerHTML =
    '<div class="cards" style="grid-template-columns:1fr 1fr;margin-bottom:0">'
    +'<div class="card"><div class="num">'+esc(d.回复他人条数||0)+'</div><div class="lbl">回复他人(条)</div></div>'
    +'<div class="card"><div class="num">'+reply+'%</div><div class="lbl">回复率</div></div>'
    +'</div>'
    +'<div class="tip">回复率 = 有引用/回复关系的消息占比。回复率越高，说明大家越爱就着别人的话接着聊，反之更偏向各自抛新话题。</div>';

  const c2=document.getElementById('chain');
  const dist=d.单话题消息条数分布||{};
  const items=Object.entries(dist).map(([k,v])=>({label:k, val:v}));
  if(d.回复链表){
    barChart(c2, items, {horizontal:true, colors:['#2a9d8f','#e9c46a','#e76f51'], suffix:' 个'});
  } else {
    c2.innerHTML='<div class="empty-note">暂无话题/回复链数据</div>';
  }
}

function showTop(){
  const d=DATA||{};
  const cards=document.getElementById('cards');
  const span=d.样本跨度小时?('约'+d.样本跨度小时+'小时'):'—';
  const items=[
    {num:esc(d.样本条数), lbl:'消息总量'},
    {num:esc(d.发言者数), lbl:'发言人数'},
    {num:esc(span), lbl:'时间跨度'},
    {num:esc(d["回复率%"]||0)+'%', lbl:'回复率'},
    {num:esc(d["夜间占比%"]!=null?d["夜间占比%"]+'%':'—'), lbl:'夜间(21-06)占比'},
  ];
  cards.innerHTML=items.map(c=>'<div class="card"><div class="num">'+c.num+'</div><div class="lbl">'+c.lbl+'</div></div>').join('');
}

async function loadAll(){
  const stamp=document.getElementById('stamp');
  stamp.textContent='加载中…';
  try{
    const r=await fetch('/api/habits?limit=2000');
    if(!r.ok) throw new Error('HTTP '+r.status);
    DATA=await r.json();
    showTop(); showSenders(); showTypes(); showLengths(); showHours(); showWeeks(); showInteraction();
    stamp.textContent='更新于 '+(new Date().toLocaleTimeString())+' · 前 '+ (DATA.样本条数||0) +' 条抽样';
  }catch(e){
    stamp.textContent='加载失败: '+e;
    document.querySelectorAll('.panel > div').forEach(el=>el.innerHTML='<div class="empty-note">加载失败：'+esc(e.message)+'</div>');
  }
}

loadAll();
