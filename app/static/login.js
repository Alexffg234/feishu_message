/* ==========================================================================
   飞书群聊知识库 · 登录 / 注册页 交互
   - 严格模式 IIFE,不污染全局
   - 密码复杂度实时校验(规则由后端下发,后端仍二次校验)
   - 键盘可达、ARIA 状态同步、加载/错误态完整
   依赖接口: POST /api/auth/login, POST /api/auth/register,
             GET  /api/auth/register(开放状态 + 密码规则)
   ========================================================================== */
(function () {
  'use strict';

  var API = {
    login: '/api/auth/login',
    register: '/api/auth/register'
  };

  // 与后端 auth_service._WEAK_PASSWORDS 对齐的常用弱密码(仅用于即时提示)
  var WEAK_LIST = [
    '12345678', '123456789', '1234567890', '87654321', '11111111', '00000000',
    'password', 'passw0rd', 'password1', 'abc12345', 'abcd1234', 'a1234567',
    'qwerty12', 'qwerty123', 'iloveyou', 'admin123', 'letmein', 'welcome1',
    'test1234', 'user1234'
  ];
  var FALLBACK_RULES = [
    '至少 8 位', '同时包含字母和数字', '不能是常见弱密码',
    '同一字符不重复 4 次以上', '不含 5 位以上连续字符'
  ];

  var MIN_LEN = 8;
  var MAX_LEN = 128;

  // ------------------------------------------------------------ DOM
  function el(id) { return document.getElementById(id); }

  var dom = {
    tabs: {
      login: el('tab-login'),
      register: el('tab-register'),
      indicator: el('tab-indicator')
    },
    alert: el('alert'),
    alertText: el('alert-text'),
    title: el('panel-title'),
    desc: el('panel-desc'),
    formLogin: el('form-login'),
    formRegister: el('form-register'),
    btnLogin: el('btn-login'),
    btnRegister: el('btn-register'),
    loginUser: el('login-user'),
    loginPass: el('login-pass'),
    regUser: el('reg-user'),
    regName: el('reg-name'),
    regPass: el('reg-pass'),
    regPass2: el('reg-pass2'),
    strength: el('strength'),
    strengthLabel: el('strength-label'),
    strengthTip: el('strength-tip'),
    rules: el('rules'),
    switchText: el('switch-text'),
    switchBtn: el('switch-btn'),
    switchRow: el('switch-row'),
    note: el('note')
  };

  var mode = 'login';
  var ruleRows = [];
  var ruleStates = [];

  // ------------------------------------------------------------ 工具
  function nextUrl() {
    var raw = new URLSearchParams(location.search).get('next') || '/summary';
    // 只接受站内相对路径,防止开放重定向
    return /^\/(?!\/)/.test(raw) ? raw : '/summary';
  }

  function setFieldError(inputId, msg) {
    var input = el(inputId);
    var box = el('err-' + inputId);
    if (box) {
      box.textContent = msg || '';
      box.hidden = !msg;
    }
    if (input) {
      input.classList.toggle('is-invalid', !!msg);
      if (msg) {
        input.setAttribute('aria-invalid', 'true');
      } else {
        input.removeAttribute('aria-invalid');
      }
    }
  }

  function clearFieldErrors(ids) {
    ids.forEach(function (id) { setFieldError(id, ''); });
    showAlert('');
  }

  function showAlert(msg) {
    dom.alertText.textContent = msg || '';
    dom.alert.hidden = !msg;
    if (msg) {
      // 重置动画以便连续报错也能触发
      dom.alert.style.animation = 'none';
      void dom.alert.offsetWidth;
      dom.alert.style.animation = '';
    }
  }

  function focusFirstInvalid(ids) {
    for (var i = 0; i < ids.length; i++) {
      var box = el('err-' + ids[i]);
      if (box && !box.hidden) {
        var input = el(ids[i]);
        if (input) { input.focus(); return; }
      }
    }
  }

  // ------------------------------------------------------------ 密码校验
  function hasLongSequence(pwd) {
    var up = 1, down = 1;
    for (var i = 1; i < pwd.length; i++) {
      var d = pwd.charCodeAt(i) - pwd.charCodeAt(i - 1);
      up = d === 1 ? up + 1 : 1;
      down = d === -1 ? down + 1 : 1;
      if (up >= 5 || down >= 5) { return true; }
    }
    return false;
  }

  function checkPassword(pwd) {
    return {
      len: pwd.length >= MIN_LEN && pwd.length <= MAX_LEN,
      mix: !!pwd && /[A-Za-z]/.test(pwd) && /\d/.test(pwd),
      weak: WEAK_LIST.indexOf(pwd.toLowerCase()) === -1,
      rep: !/(.)\1{3,}/.test(pwd),
      seq: !hasLongSequence(pwd)
    };
  }

  function passwordScore(pwd) {
    if (!pwd) { return 0; }
    var c = checkPassword(pwd);
    if (!(c.len && c.mix && c.weak && c.rep && c.seq)) { return 0; }
    var s = 1;
    if (pwd.length >= 12) { s += 1; }
    if (/[a-z]/.test(pwd) && /[A-Z]/.test(pwd)) { s += 1; }
    else if (/[^A-Za-z0-9]/.test(pwd)) { s += 1; }
    return Math.min(s, 3);
  }

  function renderRules(rules) {
    dom.rules.textContent = '';
    ruleRows = [];
    rules.forEach(function (text) {
      var li = document.createElement('li');
      li.className = 'rule';
      var mark = document.createElement('span');
      mark.className = 'rule__mark';
      mark.innerHTML = '<svg width="8" height="8" viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
        '<path d="M2 6.2 4.8 9 10 3.4" stroke="#3fc79a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      var label = document.createElement('span');
      label.textContent = text;
      li.appendChild(mark);
      li.appendChild(label);
      dom.rules.appendChild(li);
      ruleRows.push(li);
    });
  }

  function refreshPassword() {
    var pwd = dom.regPass.value;
    var c = checkPassword(pwd);
    ruleStates = [c.len, c.mix, c.weak, c.rep, c.seq];
    ruleRows.forEach(function (row, i) {
      row.classList.toggle('is-ok', !!ruleStates[i]);
    });

    var level = passwordScore(pwd);
    dom.strength.hidden = pwd.length === 0;
    dom.strength.classList.remove('is-lv1', 'is-lv2', 'is-lv3');
    if (level) { dom.strength.classList.add('is-lv' + level); }
    dom.strengthLabel.textContent = ['弱', '中', '强', '很强'][level] || '弱';

    var tips = [];
    if (pwd && !c.len) { tips.push('长度不足'); }
    if (pwd && !c.mix) { tips.push('需字母+数字'); }
    if (pwd && !c.weak) { tips.push('太常见'); }
    if (pwd && !c.rep) { tips.push('有重复字符'); }
    if (pwd && !c.seq) { tips.push('有连续字符'); }
    dom.strengthTip.textContent = tips.length ? tips.join(' · ') : (level >= 2 ? '符合建议' : '');

    dom.btnRegister.disabled = !(c.len && c.mix && c.weak && c.rep && c.seq);
    if (pwd && !dom.btnRegister.disabled) { setFieldError('reg-pass', ''); }
  }

  // ------------------------------------------------------------ 视图切换
  function moveIndicator(tab) {
    if (!tab || tab.offsetWidth === 0) { return; }
    dom.tabs.indicator.style.width = tab.offsetWidth + 'px';
    dom.tabs.indicator.style.transform = 'translateX(' + tab.offsetLeft + 'px)';
  }

  function replaySwap(form) {
    form.style.animation = 'none';
    void form.offsetWidth;
    form.style.animation = '';
  }

  function setMode(next) {
    mode = next;
    var isLogin = next === 'login';
    clearFieldErrors(['login-user', 'login-pass', 'reg-user', 'reg-pass', 'reg-pass2']);

    dom.formLogin.hidden = !isLogin;
    dom.formRegister.hidden = isLogin;
    dom.tabs.login.classList.toggle('is-active', isLogin);
    dom.tabs.register.classList.toggle('is-active', !isLogin);
    dom.tabs.login.setAttribute('aria-selected', String(isLogin));
    dom.tabs.register.setAttribute('aria-selected', String(!isLogin));
    moveIndicator(isLogin ? dom.tabs.login : dom.tabs.register);

    dom.title.textContent = isLogin ? '欢迎回来' : '创建账号';
    dom.desc.textContent = isLogin
      ? '登录后查看群聊对话与总结。'
      : '设置符合复杂度要求的密码后即可注册。';
    dom.switchText.textContent = isLogin ? '还没有账号？' : '已有账号？';
    dom.switchBtn.textContent = isLogin ? '立即注册' : '去登录';

    replaySwap(isLogin ? dom.formLogin : dom.formRegister);
    if (!isLogin) { refreshPassword(); }
    focusPrimaryInput();
  }

  function focusPrimaryInput() {
    // 移动端不自动聚焦,避免键盘立刻弹出挤压布局
    var wide = window.matchMedia('(min-width: 881px)').matches;
    if (!wide) { return; }
    var target = mode === 'login' ? dom.loginUser : dom.regUser;
    if (target && target.offsetParent !== null) { target.focus(); }
  }

  // ------------------------------------------------------------ 请求
  function post(url, body, btn) {
    var label = btn.querySelector('.btn__label');
    var spinner = document.createElement('span');
    spinner.className = 'spinner';
    spinner.setAttribute('aria-hidden', 'true');

    btn.disabled = true;
    if (label) { label.style.display = 'none'; }
    btn.appendChild(spinner);

    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, data: data };
      });
    }).then(function (result) {
      btn.removeChild(spinner);
      if (label) { label.style.display = ''; }
      btn.disabled = false;
      if (btn === dom.btnRegister) { refreshPassword(); }
      return result;
    }, function (err) {
      btn.removeChild(spinner);
      if (label) { label.style.display = ''; }
      btn.disabled = false;
      if (btn === dom.btnRegister) { refreshPassword(); }
      throw err;
    });
  }

  function handleError(detail) {
    var msg = detail || '操作失败';
    // 密码类错误就近显示在密码框下,用户名类显示在用户名下
    if (msg.indexOf('密码') === 0 || msg.indexOf('密码不满足') >= 0) {
      setFieldError('reg-pass', msg);
    } else if (msg.indexOf('用户名') >= 0) {
      setFieldError('reg-user', msg);
    } else {
      showAlert(msg);
    }
  }

  // ------------------------------------------------------------ 事件绑定
  dom.tabs.login.addEventListener('click', function () { setMode('login'); });
  dom.tabs.register.addEventListener('click', function () { setMode('register'); });
  dom.switchBtn.addEventListener('click', function () {
    setMode(mode === 'login' ? 'register' : 'login');
  });

  window.addEventListener('resize', function () {
    moveIndicator(mode === 'login' ? dom.tabs.login : dom.tabs.register);
  });

  // 密码明文切换
  Array.prototype.forEach.call(document.querySelectorAll('[data-toggle]'), function (btn) {
    btn.addEventListener('click', function () {
      var input = el(btn.getAttribute('data-toggle'));
      if (!input) { return; }
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', String(show));
      btn.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
      input.focus();
    });
  });

  // 输入即时反馈
  dom.regPass.addEventListener('input', function () {
    refreshPassword();
    if (dom.regPass2.value) {
      setFieldError('reg-pass2', dom.regPass2.value === dom.regPass.value ? '' : '两次输入的密码不一致');
    }
  });
  dom.regPass2.addEventListener('input', function () {
    var val = dom.regPass2.value;
    setFieldError('reg-pass2', val && val !== dom.regPass.value ? '两次输入的密码不一致' : '');
  });
  dom.regUser.addEventListener('input', function () { setFieldError('reg-user', ''); });
  dom.loginUser.addEventListener('input', function () { setFieldError('login-user', ''); });
  dom.loginPass.addEventListener('input', function () { setFieldError('login-pass', ''); });

  // 登录提交
  dom.formLogin.addEventListener('submit', function (e) {
    e.preventDefault();
    clearFieldErrors(['login-user', 'login-pass']);
    var body = {
      username: dom.loginUser.value.trim(),
      password: dom.loginPass.value
    };
    var bad = false;
    if (!body.username) { setFieldError('login-user', '请输入用户名'); bad = true; }
    if (!body.password) { setFieldError('login-pass', '请输入密码'); bad = true; }
    if (bad) { focusFirstInvalid(['login-user', 'login-pass']); return; }

    post(API.login, body, dom.btnLogin).then(function (res) {
      if (!res.ok) { showAlert(res.data.detail || '登录失败'); return; }
      location.assign(nextUrl());
    }).catch(function () { showAlert('网络异常，请重试'); });
  });

  // 注册提交
  dom.formRegister.addEventListener('submit', function (e) {
    e.preventDefault();
    clearFieldErrors(['reg-user', 'reg-pass', 'reg-pass2']);
    var body = {
      username: dom.regUser.value.trim(),
      display_name: dom.regName.value.trim(),
      password: dom.regPass.value
    };
    var bad = false;
    if (!body.username || body.username.length < 3) {
      setFieldError('reg-user', '用户名至少 3 位'); bad = true;
    }
    var c = checkPassword(body.password);
    if (!c.len && !c.mix) {
      setFieldError('reg-pass', '密码至少 8 位且同时包含字母和数字'); bad = true;
    } else if (!(c.len && c.mix && c.weak && c.rep && c.seq)) {
      setFieldError('reg-pass', '密码不符合下方列出的要求'); bad = true;
    }
    if (dom.regPass2.value !== body.password) {
      setFieldError('reg-pass2', '两次输入的密码不一致'); bad = true;
    }
    if (bad) {
      refreshPassword();
      focusFirstInvalid(['reg-user', 'reg-pass', 'reg-pass2']);
      return;
    }

    post(API.register, body, dom.btnRegister).then(function (res) {
      if (!res.ok) { handleError(res.data.detail); return; }
      location.assign(nextUrl());
    }).catch(function () { showAlert('网络异常，请重试'); });
  });

  // ------------------------------------------------------------ 初始化
  renderRules(FALLBACK_RULES);
  refreshPassword();
  moveIndicator(dom.tabs.login);

  fetch(API.register, { method: 'GET' }).then(function (r) {
    return r.json();
  }).then(function (data) {
    if (data && Array.isArray(data.password_rules) && data.password_rules.length) {
      renderRules(data.password_rules);
      refreshPassword();
    }
    if (data && data.min_len) { MIN_LEN = data.min_len; }
    if (data && data.max_len) { MAX_LEN = data.max_len; }

    if (data && data.open === false) {
      // 自助注册关闭:只保留登录
      dom.tabs.register.hidden = true;
      dom.switchRow.hidden = true;
      setMode('login');
      moveIndicator(dom.tabs.login);
      dom.note.textContent = '自助注册已关闭，请联系管理员开通账号。';
    }
  }).catch(function () {
    // 接口不可达时保持默认(规则用内置文案),不影响登录
  });

  focusPrimaryInput();
})();
