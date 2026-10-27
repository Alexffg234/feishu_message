"""登录 / 注册页面(内建 HTML,无构建依赖)。

访问 GET /login。页面在登录与注册两个表单间切换,通过
POST /api/auth/login 与 POST /api/auth/register 提交。

浅色主题,配色与 /summary、/habits 保持一致。
样式要点:柔和光晕背景 + 分栏卡片;输入框带图标前缀与焦点环;
密码框支持明文切换;按钮带加载旋转;错误提示带滑入/抖动动效;
移动端折叠品牌区为顶部紧凑 logo。
"""

LOGIN_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · 飞书群聊知识库</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23185fa5'/%3E%3Cpath d='M9 12h14M9 17h9' stroke='white' stroke-width='2.2' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
  :root{
    --bg:#eef1f6; --card:#ffffff; --border:#e3e6ea;
    --text:#1f2329; --muted:#6b7280; --faint:#9aa1ab;
    --primary:#185fa5; --primary-hover:#12497f; --primary-dark:#0c447c;
    --primary-soft:rgba(24,95,165,.12);
    --accent:#0f6e56; --danger:#a32d2d; --danger-bg:#fcebeb;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text);line-height:1.6;
    display:flex;align-items:center;justify-content:center;padding:36px 20px;
    background:
      radial-gradient(620px 420px at 12% -8%, rgba(24,95,165,.10), transparent 60%),
      radial-gradient(560px 400px at 100% 108%, rgba(15,110,86,.09), transparent 60%),
      var(--bg);
    min-height:100vh;
  }

  .mobile-logo{display:none}

  /* ---------------- 卡片外壳 ---------------- */
  .shell{
    width:100%;max-width:900px;display:flex;background:var(--card);
    border:1px solid rgba(228,232,238,.9);border-radius:22px;overflow:hidden;
    box-shadow:
      0 1px 2px rgba(15,23,42,.04),
      0 12px 32px rgba(15,23,42,.07),
      0 32px 72px rgba(15,23,42,.08);
    animation:enter .45s cubic-bezier(.22,.8,.35,1) both;
  }
  @keyframes enter{
    from{opacity:0;transform:translateY(14px)}
    to{opacity:1;transform:none}
  }
  @media (prefers-reduced-motion:reduce){
    .shell{animation:none}
  }

  /* ---------------- 左侧品牌区 ---------------- */
  .brand{
    flex:0 0 360px;position:relative;overflow:hidden;color:#fff;
    padding:46px 40px;display:flex;flex-direction:column;justify-content:space-between;
    background:linear-gradient(152deg,#0c447c 0%,#14568f 55%,#185fa5 100%);
  }
  .brand::before{ /* 细网格纹理 */
    content:"";position:absolute;inset:0;opacity:.5;
    background-image:
      linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),
      linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px);
    background-size:30px 30px;
  }
  .brand::after{ /* 右下光晕 */
    content:"";position:absolute;right:-120px;bottom:-140px;width:340px;height:340px;
    border-radius:50%;
    background:radial-gradient(circle,rgba(93,202,165,.22) 0%,transparent 68%);
    pointer-events:none;
  }
  .brand>*{position:relative;z-index:1}
  .logo{display:flex;align-items:center;gap:12px}
  .logo-mark{
    width:38px;height:38px;border-radius:11px;flex:none;
    background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.18);
    display:flex;align-items:center;justify-content:center;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.15);
  }
  .logo-mark svg{display:block}
  .logo-name{font-size:15.5px;font-weight:600;letter-spacing:.3px}
  .brand-main{margin-top:8px;margin-bottom:8px}
  .brand-title{font-size:27px;font-weight:600;line-height:1.4;margin:40px 0 14px;letter-spacing:.5px}
  .brand-sub{font-size:14px;color:rgba(255,255,255,.75);line-height:1.8;max-width:270px}
  .brand-list{margin-top:32px;display:flex;flex-direction:column;gap:15px}
  .brand-item{display:flex;gap:11px;align-items:flex-start;font-size:13.5px;color:rgba(255,255,255,.88)}
  .check{
    width:19px;height:19px;border-radius:50%;flex:none;margin-top:3px;
    background:rgba(93,202,165,.18);border:1px solid rgba(93,202,165,.35);
    display:flex;align-items:center;justify-content:center;
  }
  .check svg{display:block}
  .brand-foot{
    font-size:12px;color:rgba(255,255,255,.45);margin-top:36px;
    display:flex;align-items:center;gap:8px;
  }
  .brand-foot::before{
    content:"";width:18px;height:1px;background:rgba(255,255,255,.25);display:inline-block;
  }

  /* ---------------- 右侧表单区 ---------------- */
  .panel{flex:1;padding:46px 46px;display:flex;flex-direction:column;justify-content:center;min-width:0}
  .tabs{
    display:flex;gap:28px;border-bottom:1px solid var(--border);margin-bottom:30px;
  }
  .tab{
    background:none;border:none;padding:0 0 13px;font-size:15px;color:var(--faint);
    cursor:pointer;font-family:inherit;position:relative;margin-bottom:-1px;
    border-bottom:2px solid transparent;transition:color .18s;
  }
  .tab:hover{color:var(--muted)}
  .tab.active{color:var(--text);font-weight:600;border-bottom-color:var(--primary)}

  .head{font-size:21px;font-weight:600;margin-bottom:6px;letter-spacing:.2px}
  .desc{font-size:13.5px;color:var(--muted);margin-bottom:26px}

  .field{margin-bottom:17px}
  .field label{display:block;font-size:13px;color:var(--muted);margin-bottom:7px;font-weight:500}
  .control{position:relative}
  .control .lead{
    position:absolute;left:13px;top:50%;transform:translateY(-50%);
    color:var(--faint);pointer-events:none;display:flex;transition:color .18s;
  }
  .control input{
    width:100%;padding:12px 14px 12px 41px;font-size:14px;font-family:inherit;color:var(--text);
    border:1px solid var(--border);border-radius:10px;background:#fafbfc;outline:none;
    transition:border-color .18s,box-shadow .18s,background .18s;
  }
  .control input.has-toggle{padding-right:42px}
  .control input::placeholder{color:#b3b9c2}
  .control input:hover{border-color:#cfd5dc}
  .control input:focus{
    border-color:var(--primary);background:#fff;
    box-shadow:0 0 0 3.5px var(--primary-soft);
  }
  .control:focus-within .lead{color:var(--primary)}
  /* 覆盖 webkit 自动填充的蓝色底 */
  .control input:-webkit-autofill{
    -webkit-box-shadow:0 0 0 1000px #fff inset;
    transition:background-color 99999s ease-in-out 0s;
  }
  .toggle-pass{
    position:absolute;right:6px;top:50%;transform:translateY(-50%);
    width:32px;height:32px;border:none;border-radius:8px;background:transparent;
    color:var(--faint);cursor:pointer;display:flex;align-items:center;justify-content:center;
    transition:color .18s,background .18s;
  }
  .toggle-pass:hover{color:var(--muted);background:#f1f3f6}
  .toggle-pass svg{display:none}
  .toggle-pass svg.on{display:block}

  .hint{font-size:12px;color:var(--faint);margin-top:7px;min-height:1em}

  .submit{
    width:100%;padding:12.5px;margin-top:8px;border:none;border-radius:10px;
    background:var(--primary);color:#fff;font-size:15px;font-weight:500;
    font-family:inherit;cursor:pointer;letter-spacing:6px;text-indent:6px;
    box-shadow:0 6px 16px rgba(24,95,165,.22);
    transition:background .18s,transform .12s,box-shadow .18s;
    display:flex;align-items:center;justify-content:center;gap:9px;height:46px;
  }
  .submit:hover{background:var(--primary-hover);transform:translateY(-1px);
    box-shadow:0 9px 20px rgba(24,95,165,.28)}
  .submit:active{transform:none;box-shadow:0 4px 10px rgba(24,95,165,.22)}
  .submit:disabled{opacity:.65;cursor:default;transform:none}
  .spin{
    width:16px;height:16px;border-radius:50%;flex:none;
    border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
    animation:spin .7s linear infinite;
  }
  @keyframes spin{to{transform:rotate(360deg)}}

  .alert{
    display:none;align-items:flex-start;gap:9px;
    padding:11px 14px;border-radius:10px;font-size:13px;
    background:var(--danger-bg);color:var(--danger);margin-bottom:18px;
    border:1px solid rgba(163,45,45,.14);
  }
  .alert.show{display:flex;animation:shake .35s ease}
  .alert svg{flex:none;margin-top:3px}
  @keyframes shake{
    0%,100%{transform:translateX(0)}
    25%{transform:translateX(-4px)}
    60%{transform:translateX(3px)}
  }

  .footer{margin-top:24px;font-size:13.5px;color:var(--muted);text-align:center}
  .footer button{
    background:none;border:none;color:var(--primary);cursor:pointer;
    font-family:inherit;font-size:13.5px;padding:0 0 0 2px;font-weight:500;
  }
  .footer button:hover{text-decoration:underline}
  .note{margin-top:24px;font-size:12px;color:var(--faint);text-align:center;line-height:1.7}

  /* ---------------- 移动端 ---------------- */
  @media (max-width:840px){
    body{padding:22px 16px;align-items:flex-start}
    .mobile-logo{
      display:flex;align-items:center;gap:10px;margin:6px 0 18px;
      color:var(--primary-dark);font-weight:600;font-size:15px;
    }
    .mobile-logo .logo-mark{
      width:32px;height:32px;border-radius:9px;
      background:var(--primary);border:none;
      display:flex;align-items:center;justify-content:center;
    }
    .brand{display:none}
    .shell{max-width:460px}
    .panel{padding:34px 28px 30px}
    .submit{letter-spacing:4px;text-indent:4px}
  }
</style>
</head>
<body>
<div class="mobile-logo">
  <span class="logo-mark">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
      <path d="M4 7h16M4 12h11" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
    </svg>
  </span>
  <span>飞书群聊知识库</span>
</div>

<div class="shell">
  <aside class="brand">
    <div class="logo">
      <span class="logo-mark">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none">
          <path d="M4 7h16M4 12h11" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
        </svg>
      </span>
      <span class="logo-name">飞书群聊知识库</span>
    </div>
    <div class="brand-main">
      <h1 class="brand-title">把群聊沉淀成<br>可检索的问题库</h1>
      <p class="brand-sub">自动采集群消息,还原回复链,归纳问题主题并生成排查与解决建议。</p>
      <div class="brand-list">
        <div class="brand-item">
          <span class="check"><svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M2 6.2 4.8 9 10 3.4" stroke="#5dcaa5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
          <span>实时采集机器人所在群的全部消息</span>
        </div>
        <div class="brand-item">
          <span class="check"><svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M2 6.2 4.8 9 10 3.4" stroke="#5dcaa5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
          <span>回复链还原成完整对话树</span>
        </div>
        <div class="brand-item">
          <span class="check"><svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M2 6.2 4.8 9 10 3.4" stroke="#5dcaa5" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
          <span>按主题聚合并生成 AI 分析</span>
        </div>
      </div>
    </div>
    <div class="brand-foot">Feishu Group Message Capture</div>
  </aside>

  <main class="panel">
    <nav class="tabs">
      <button class="tab active" id="tab-login" type="button">登录</button>
      <button class="tab" id="tab-register" type="button">注册</button>
    </nav>

    <div id="alert" class="alert">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
        <circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/>
        <path d="M8 4.6v4.2M8 11.2v.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
      <span id="alert-text"></span>
    </div>

    <h2 class="head" id="head">欢迎回来</h2>
    <p class="desc" id="desc">登录后查看群聊对话与总结。</p>

    <form id="form-login" autocomplete="on" novalidate>
      <div class="field">
        <label for="login-user">用户名</label>
        <div class="control">
          <span class="lead">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <circle cx="8" cy="5.4" r="2.9" stroke="currentColor" stroke-width="1.5"/>
              <path d="M2.6 13.6c.7-2.5 2.8-3.9 5.4-3.9s4.7 1.4 5.4 3.9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </span>
          <input id="login-user" name="username" type="text" autocomplete="username" placeholder="请输入用户名">
        </div>
      </div>
      <div class="field">
        <label for="login-pass">密码</label>
        <div class="control">
          <span class="lead">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <rect x="3" y="7" width="10" height="7" rx="1.8" stroke="currentColor" stroke-width="1.5"/>
              <path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </span>
          <input id="login-pass" name="password" type="password" autocomplete="current-password" class="has-toggle" placeholder="请输入密码">
          <button type="button" class="toggle-pass" data-for="login-pass" title="显示/隐藏密码" aria-label="显示或隐藏密码">
            <svg class="on" viewBox="0 0 18 18" width="17" height="17" fill="none" data-mode="hide">
              <path d="M1.8 9S4.6 4.4 9 4.4 16.2 9 16.2 9 13.4 13.6 9 13.6 1.8 9 1.8 9Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
              <circle cx="9" cy="9" r="2.1" stroke="currentColor" stroke-width="1.4"/>
              <path d="m3 15 12-12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
            </svg>
            <svg class="on" viewBox="0 0 18 18" width="17" height="17" fill="none" data-mode="show" style="display:none">
              <path d="M1.8 9S4.6 4.4 9 4.4 16.2 9 16.2 9 13.4 13.6 9 13.6 1.8 9 1.8 9Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
              <circle cx="9" cy="9" r="2.1" stroke="currentColor" stroke-width="1.4"/>
            </svg>
          </button>
        </div>
      </div>
      <button class="submit" id="btn-login" type="submit"><span class="label">登录</span></button>
    </form>

    <form id="form-register" style="display:none" autocomplete="on" novalidate>
      <div class="field">
        <label for="reg-user">用户名</label>
        <div class="control">
          <span class="lead">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <circle cx="8" cy="5.4" r="2.9" stroke="currentColor" stroke-width="1.5"/>
              <path d="M2.6 13.6c.7-2.5 2.8-3.9 5.4-3.9s4.7 1.4 5.4 3.9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </span>
          <input id="reg-user" name="username" type="text" autocomplete="username" placeholder="3~32 位字母、数字或中文">
        </div>
      </div>
      <div class="field">
        <label for="reg-name">昵称<span style="color:var(--faint);font-weight:400">（选填）</span></label>
        <div class="control">
          <span class="lead">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5"/>
              <path d="M8 4.8v3.4l2.3 1.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <input id="reg-name" name="display_name" type="text" placeholder="展示用名称,留空同用户名">
        </div>
      </div>
      <div class="field">
        <label for="reg-pass">密码</label>
        <div class="control">
          <span class="lead">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <rect x="3" y="7" width="10" height="7" rx="1.8" stroke="currentColor" stroke-width="1.5"/>
              <path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </span>
          <input id="reg-pass" name="password" type="password" autocomplete="new-password" class="has-toggle" placeholder="至少 8 位">
          <button type="button" class="toggle-pass" data-for="reg-pass" title="显示/隐藏密码" aria-label="显示或隐藏密码">
            <svg class="on" viewBox="0 0 18 18" width="17" height="17" fill="none" data-mode="hide">
              <path d="M1.8 9S4.6 4.4 9 4.4 16.2 9 16.2 9 13.4 13.6 9 13.6 1.8 9 1.8 9Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
              <circle cx="9" cy="9" r="2.1" stroke="currentColor" stroke-width="1.4"/>
              <path d="m3 15 12-12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
            </svg>
            <svg class="on" viewBox="0 0 18 18" width="17" height="17" fill="none" data-mode="show" style="display:none">
              <path d="M1.8 9S4.6 4.4 9 4.4 16.2 9 16.2 9 13.4 13.6 9 13.6 1.8 9 1.8 9Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
              <circle cx="9" cy="9" r="2.1" stroke="currentColor" stroke-width="1.4"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="field">
        <label for="reg-pass2">确认密码</label>
        <div class="control">
          <span class="lead">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <rect x="3" y="7" width="10" height="7" rx="1.8" stroke="currentColor" stroke-width="1.5"/>
              <path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </span>
          <input id="reg-pass2" type="password" autocomplete="new-password" placeholder="再次输入密码">
        </div>
        <div class="hint" id="reg-hint"></div>
      </div>
      <button class="submit" id="btn-register" type="submit"><span class="label">注册</span></button>
    </form>

    <div class="footer" id="footer">
      <span id="footer-text">还没有账号?</span>
      <button type="button" id="btn-switch">立即注册</button>
    </div>
    <div class="note" id="note"></div>
  </main>
</div>

<script>
(function(){
  var params = new URLSearchParams(location.search);
  var next = params.get('next') || '/summary';
  // 只接受站内相对路径,防止开放重定向
  if (!/^\/(?!\/)/.test(next)) next = '/summary';

  var mode = 'login';
  var alertBox = document.getElementById('alert');
  var alertText = document.getElementById('alert-text');
  var head = document.getElementById('head');
  var desc = document.getElementById('desc');
  var footerText = document.getElementById('footer-text');
  var btnSwitch = document.getElementById('btn-switch');
  var formLogin = document.getElementById('form-login');
  var formReg = document.getElementById('form-register');
  var tabLogin = document.getElementById('tab-login');
  var tabReg = document.getElementById('tab-register');

  function showError(msg){
    alertText.textContent = msg || '';
    // 重新触发抖动动画
    alertBox.classList.remove('show');
    if(msg){
      void alertBox.offsetWidth;
      alertBox.classList.add('show');
    }
  }

  function setMode(m){
    mode = m;
    showError('');
    var isLogin = m === 'login';
    formLogin.style.display = isLogin ? '' : 'none';
    formReg.style.display = isLogin ? 'none' : '';
    tabLogin.classList.toggle('active', isLogin);
    tabReg.classList.toggle('active', !isLogin);
    head.textContent = isLogin ? '欢迎回来' : '创建账号';
    desc.textContent = isLogin ? '登录后查看群聊对话与总结。' : '注册后即可访问全部群聊数据。';
    footerText.textContent = isLogin ? '还没有账号?' : '已有账号?';
    btnSwitch.textContent = isLogin ? '立即注册' : '去登录';
  }

  tabLogin.onclick = function(){ setMode('login'); };
  tabReg.onclick = function(){ setMode('register'); };
  btnSwitch.onclick = function(){ setMode(mode === 'login' ? 'register' : 'login'); };

  // 密码明文切换(注册页确认密码不提供切换,鼓励盲输一致)
  Array.prototype.forEach.call(document.querySelectorAll('.toggle-pass'), function(btn){
    btn.onclick = function(){
      var input = document.getElementById(btn.getAttribute('data-for'));
      if(!input) return;
      var showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      Array.prototype.forEach.call(btn.querySelectorAll('svg'), function(svg){
        var m = svg.getAttribute('data-mode');
        svg.style.display = (m === 'hide') === showing ? 'block' : 'none';
      });
      input.focus();
    };
  });

  // 注册密码实时强度提示
  var regPass = document.getElementById('reg-pass');
  var regHint = document.getElementById('reg-hint');
  regPass && (regPass.oninput = function(){
    var v = regPass.value;
    if(!v){ regHint.textContent = ''; return; }
    if(v.length < 8){ regHint.textContent = '已输入 ' + v.length + ' 位,还需 ' + (8 - v.length) + ' 位'; }
    else if(/^\d+$/.test(v) || /^[a-z]+$/i.test(v)){ regHint.textContent = '已满足长度,建议混合字母与数字更安全'; }
    else { regHint.textContent = '密码强度不错'; }
  });

  function post(url, body, btn){
    var label = btn.querySelector('.label');
    var spinner = document.createElement('span');
    spinner.className = 'spin';
    btn.disabled = true;
    label.style.display = 'none';
    btn.appendChild(spinner);
    return fetch(url, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    }).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(data){
        return { ok: r.ok, data: data };
      });
    }).finally(function(){
      btn.removeChild(spinner);
      label.style.display = '';
      btn.disabled = false;
    });
  }

  formLogin.onsubmit = function(e){
    e.preventDefault();
    showError('');
    var body = {
      username: document.getElementById('login-user').value.trim(),
      password: document.getElementById('login-pass').value
    };
    if(!body.username || !body.password){ showError('请输入用户名和密码'); return; }
    post('/api/auth/login', body, document.getElementById('btn-login')).then(function(res){
      if(!res.ok){ showError(res.data.detail || '登录失败'); return; }
      location.assign(next);
    }).catch(function(){ showError('网络异常,请重试'); });
  };

  formReg.onsubmit = function(e){
    e.preventDefault();
    showError('');
    regHint.textContent = '';
    var body = {
      username: document.getElementById('reg-user').value.trim(),
      display_name: document.getElementById('reg-name').value.trim(),
      password: document.getElementById('reg-pass').value
    };
    var pass2 = document.getElementById('reg-pass2').value;
    if(body.password !== pass2){ showError('两次输入的密码不一致'); return; }
    post('/api/auth/register', body, document.getElementById('btn-register')).then(function(res){
      if(!res.ok){ showError(res.data.detail || '注册失败'); return; }
      location.assign(next);
    }).catch(function(){ showError('网络异常,请重试'); });
  };

  // 注册未开放时(ALLOW_PUBLIC_REGISTER=false)隐藏注册入口
  fetch('/api/auth/register').then(function(r){ return r.json(); }).then(function(data){
    var open = !!(data && data.open);
    if(!open){
      tabReg.style.display = 'none';
      document.getElementById('footer').style.display = 'none';
      setMode('login');
      document.getElementById('note').textContent =
        '自助注册已关闭,请联系管理员开通账号。';
    }
  }).catch(function(){});
})();
</script>
</body>
</html>
"""
