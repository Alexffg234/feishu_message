/* ==========================================================================
   飞书群聊知识库 · 全局顶部导航
   - 由 JS 动态渲染,全站一处维护(summary / habits 等页面只放挂载点)
   - 展示当前登录用户(GET /api/auth/me),提供退出(POST /api/auth/logout)
   - 当前页面自动高亮;移动端链接换行可横向滚动
   用法: 页面 <body> 顶部放置 <div id="app-nav"></div>,并引入本脚本
   ========================================================================== */
(function () {
  'use strict';

  var LINKS = [
    { href: '/summary', text: '群聊总结' },
    { href: '/habits', text: '对话习惯' }
  ];

  var LOGO_SVG =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
    '<path d="M4 7h16M4 12h11" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>';

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text != null) { node.textContent = text; }
    return node;
  }

  function isActive(href) {
    return location.pathname === href ||
      location.pathname.indexOf(href + '/') === 0;
  }

  /* ---- 会话空闲倒计时 -------------------------------------------------
     后端在每次有效请求时滑动续期会话(默认 10 分钟空闲即失效);
     /api/auth/me 属于白名单,不会续期,所以可以安全用于查询剩余时间。
     倒计时归零时先复核一次:确实过期 → 跳登录页;仍有效(说明期间有
     其他请求续期过)→ 重新对齐倒计时,避免误踢。
  ------------------------------------------------------------------- */
  var timer = null;
  var left = 0;

  function fmt(sec) {
    var s = Math.max(0, Math.floor(sec));
    var m = Math.floor(s / 60);
    return m + ':' + ('0' + (s % 60)).slice(-2);
  }

  function paintCountdown(node) {
    var t = fmt(left);
    node.textContent = t + ' 后自动退出';
    node.title = '长时间无操作会自动退出登录';
    node.classList.toggle('is-warning', left <= 60);
  }

  function goLogin() {
    var next = location.pathname + location.search;
    location.assign('/login?next=' + encodeURIComponent(next));
  }

  function startCountdown(node, seconds) {
    if (timer) { clearInterval(timer); timer = null; }
    if (!node || !seconds || seconds <= 0) { return; }
    left = seconds;
    paintCountdown(node);
    timer = setInterval(function () {
      left -= 1;
      paintCountdown(node);
      if (left > 0) { return; }
      clearInterval(timer); timer = null;
      // 归零:复核服务端真实状态,避免误踢
      fetch('/api/auth/me', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (u) {
          if (u && u.expires_in > 1) { startCountdown(node, u.expires_in); }
          else { goLogin(); }
        })
        .catch(function () { goLogin(); });
    }, 1000);
  }

  function initial(name) {
    var n = (name || '?').trim();
    return n ? n.charAt(0).toUpperCase() : '?';
  }

  function render(user) {
    var root = document.getElementById('app-nav');
    if (!root) { return; }

    var nav = el('header', 'app-nav');
    nav.setAttribute('role', 'banner');

    // 品牌
    var brand = el('a', 'app-nav__brand');
    brand.href = '/summary';
    var mark = el('span', 'app-nav__mark');
    mark.innerHTML = LOGO_SVG;
    brand.appendChild(mark);
    brand.appendChild(el('span', null, '飞书群聊知识库'));
    nav.appendChild(brand);

    // 页面链接
    var links = el('nav', 'app-nav__links');
    links.setAttribute('aria-label', '页面导航');
    LINKS.forEach(function (item) {
      var a = el('a', 'app-nav__link', item.text);
      a.href = item.href;
      if (isActive(item.href)) {
        a.classList.add('is-active');
        a.setAttribute('aria-current', 'page');
      }
      links.appendChild(a);
    });
    nav.appendChild(links);

    nav.appendChild(el('div', 'app-nav__spacer'));

    // 用户信息
    var userBox = el('div', 'app-nav__user');
    var avatar = el('span', 'app-nav__avatar', initial(user && user.display_name));
    avatar.setAttribute('aria-hidden', 'true');
    userBox.appendChild(avatar);
    userBox.appendChild(el('span', 'app-nav__name', (user && user.display_name) || '未登录'));
    if (user && user.role) {
      userBox.appendChild(el('span', 'app-nav__role', user.role === 'admin' ? '管理员' : '成员'));
    }
    // 会话状态:剩余有效时间倒计时
    var countdown = null;
    if (user && user.expires_in > 0) {
      countdown = el('span', 'app-nav__session');
      userBox.appendChild(countdown);
    }
    nav.appendChild(userBox);

    // 退出
    var logout = el('button', 'app-nav__logout', '退出登录');
    logout.type = 'button';
    logout.addEventListener('click', function () {
      logout.disabled = true;
      logout.textContent = '退出中…';
      fetch('/api/auth/logout', { method: 'POST' })
        .then(function () { location.assign('/login'); })
        .catch(function () { location.assign('/login'); });
    });
    nav.appendChild(logout);

    root.appendChild(nav);
    startCountdown(countdown, user && user.expires_in);
  }

  function boot() {
    // 登录页没有挂载点,直接跳过
    if (!document.getElementById('app-nav')) { return; }
    fetch('/api/auth/me', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (user) { render(user); })
      .catch(function () { render(null); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
