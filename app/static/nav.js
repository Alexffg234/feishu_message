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
