/* 会话列表的共用渲染与操作（pc.html / h5.html 各引用一次）。
 *
 * 两页的差异只有三件事：列表挂到哪个容器、切换会话后怎么重连、出错怎么提示。
 * 其余（分组、悬停菜单、原地重命名、删除确认、搜索过滤）完全一致，所以放在这里，
 * 避免两份代码各自演化。
 *
 * 身份与归属判定全部在服务端：本模块只把当前 token 带上，绝不把 customer_id
 * 放进请求，也不做任何"这条会话是不是我的"的本地推断。
 */
(function (global) {
  'use strict';

  var state = { list: [], keyword: '', editing: null, confirming: null, openMenu: null };
  var opts = null;

  function displayName(item) {
    return item.title || item.preview || '（空会话）';
  }

  function bucket(iso) {
    if (!iso) return '更早';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '更早';
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var diff = Math.round((today - that) / 86400000);
    if (diff <= 0) return '今天';
    if (diff === 1) return '昨天';
    if (diff < 7) return '近 7 天';
    return '更早';
  }

  function clock(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    if (d.toDateString() === new Date().toDateString()) {
      return pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    return (d.getMonth() + 1) + '/' + d.getDate();
  }

  function session() {
    return opts.getSession();
  }

  function request(path, options) {
    return opts.request(path, options);
  }

  function notify(msg) {
    if (opts.notify) opts.notify(msg);
  }

  function render() {
    var box = opts.listEl;
    if (!box) return;
    box.textContent = '';
    var s = session();

    if (s && s.is_guest) {
      var tip = document.createElement('div');
      tip.className = 'conv-empty';
      tip.textContent = '游客会话 2 小时后失效，登录后可保留历史';
      box.appendChild(tip);
      return;
    }

    var kw = state.keyword.trim().toLowerCase();
    var items = state.list.filter(function (it) {
      if (!kw) return true;
      return (it.title || it.preview || '').toLowerCase().indexOf(kw) >= 0;
    });
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'conv-empty';
      empty.textContent = kw ? '没有匹配的会话' : '还没有历史会话';
      box.appendChild(empty);
      return;
    }

    var lastBucket = null;
    var section = null;
    items.forEach(function (it) {
      var b = bucket(it.last_active_at);
      if (b !== lastBucket) {
        // 每组套一个 section：sticky 的边界是"自己的容器"，否则三个分组标题
        // 会同时停在 top:0 互相盖住，滚动时看到的标题和实际所在分组对不上。
        section = document.createElement('div');
        section.className = 'conv-section';
        var group = document.createElement('div');
        group.className = 'conv-group';
        group.textContent = b;
        section.appendChild(group);
        box.appendChild(section);
        lastBucket = b;
      }
      section.appendChild(renderItem(it));
      if (state.confirming === it.thread_id) section.appendChild(renderConfirm(it));
    });
  }

  function renderItem(it) {
    var row = document.createElement('div');
    row.className = 'conv-item' + (it.current ? ' active' : '');

    if (state.editing === it.thread_id) {
      var input = document.createElement('input');
      input.className = 'conv-rename';
      input.value = it.title || '';
      input.placeholder = it.preview || '会话名称';
      input.maxLength = 60;
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); rename(it.thread_id, input.value); }
        if (e.key === 'Escape') { state.editing = null; render(); }
      });
      input.addEventListener('blur', function () { rename(it.thread_id, input.value); });
      row.appendChild(input);
      setTimeout(function () { input.focus(); input.select(); }, 0);
      return row;
    }

    var title = document.createElement('div');
    title.className = 'conv-item-title';
    title.textContent = displayName(it);
    row.appendChild(title);

    if (it.has_pending_turn) {
      var dot = document.createElement('span');
      dot.className = 'conv-item-dot';
      dot.title = '上一轮仍在处理';
      row.appendChild(dot);
    } else {
      var time = document.createElement('div');
      time.className = 'conv-item-time';
      time.textContent = clock(it.last_active_at);
      row.appendChild(time);
    }

    var more = document.createElement('button');
    more.type = 'button';
    more.className = 'conv-item-more';
    more.textContent = '⋯';
    more.title = '更多操作';
    more.addEventListener('click', function (e) {
      e.stopPropagation();
      state.openMenu = state.openMenu === it.thread_id ? null : it.thread_id;
      render();
    });
    row.appendChild(more);

    row.addEventListener('click', function () {
      if (it.current) return;
      state.openMenu = null;
      select(it.thread_id);
    });

    if (state.openMenu === it.thread_id) row.appendChild(renderMenu(it));
    return row;
  }

  function renderMenu(it) {
    var menu = document.createElement('div');
    menu.className = 'conv-menu';

    var ren = document.createElement('button');
    ren.type = 'button';
    ren.textContent = '重命名';
    ren.addEventListener('click', function (e) {
      e.stopPropagation();
      state.openMenu = null;
      state.editing = it.thread_id;
      render();
    });

    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'danger';
    del.textContent = '删除';
    del.addEventListener('click', function (e) {
      e.stopPropagation();
      state.openMenu = null;
      state.confirming = it.thread_id;
      render();
    });

    menu.appendChild(ren);
    // 当前会话不给删除入口：服务端会返回 409（前端拿着失效 thread_id 会继续发消息）
    if (!it.current) menu.appendChild(del);
    return menu;
  }

  function renderConfirm(it) {
    var box = document.createElement('div');
    box.className = 'conv-confirm';

    var text = document.createElement('div');
    text.className = 'conv-confirm-text';
    text.textContent = '删除「' + displayName(it) + '」？';

    var hint = document.createElement('div');
    hint.className = 'conv-confirm-hint';
    hint.textContent = '聊天记录将从列表移除，无法恢复';

    var actions = document.createElement('div');
    actions.className = 'conv-confirm-actions';

    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', function (e) {
      e.stopPropagation();
      state.confirming = null;
      render();
    });

    var ok = document.createElement('button');
    ok.type = 'button';
    ok.className = 'danger';
    ok.textContent = '删除';
    ok.addEventListener('click', function (e) {
      e.stopPropagation();
      remove(it.thread_id);
    });

    actions.appendChild(cancel);
    actions.appendChild(ok);
    box.appendChild(text);
    box.appendChild(hint);
    box.appendChild(actions);
    return box;
  }

  function load() {
    var s = session();
    if (!s || !s.token || s.is_guest) {
      render();
      if (opts.onLoaded) opts.onLoaded([]);
      return Promise.resolve();
    }
    return request('/api/customer/conversations', {
      headers: { 'Authorization': 'Bearer ' + s.token },
      cache: 'no-store'
    }).then(function (resp) {
      if (!resp.ok) return null;
      return resp.json();
    }).then(function (body) {
      if (body && body.code === 0 && body.data) {
        state.list = body.data.conversations || [];
        if (opts.footHintEl) {
          opts.footHintEl.textContent = '会话保留 ' + (body.data.retention_days || 7) + ' 天';
        }
      }
    }).catch(function () {
      /* 列表拉不到不该挡住聊天 */
    }).then(function () {
      render();
      if (opts.onLoaded) opts.onLoaded(state.list);
    });
  }

  function select(threadId) {
    opts.onSwitch('/api/customer/conversations/' + encodeURIComponent(threadId) + '/activate');
  }

  function create() {
    opts.onSwitch('/api/customer/conversations');
  }

  function rename(threadId, value) {
    if (state.editing !== threadId) return;
    state.editing = null;
    var s = session();
    if (!s) return;
    var title = (value || '').trim();
    var item = null;
    state.list.forEach(function (x) { if (x.thread_id === threadId) item = x; });
    var before = item ? item.title : '';
    if (item) item.title = title;
    render();
    request('/api/customer/conversations/' + encodeURIComponent(threadId), {
      method: 'PATCH',
      headers: { 'Authorization': 'Bearer ' + s.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title })
    }).then(function (resp) {
      return resp.json().then(function (body) { return { ok: resp.ok, body: body }; })
        .catch(function () { return { ok: resp.ok, body: null }; });
    }).then(function (res) {
      if (!res.ok || !res.body || res.body.code !== 0) {
        if (item) item.title = before;
        render();
        notify((res.body && res.body.msg) || '重命名失败');
      }
    }).catch(function () {
      if (item) item.title = before;
      render();
      notify('重命名失败，请稍后再试');
    });
  }

  function remove(threadId) {
    state.confirming = null;
    var s = session();
    if (!s) return;
    request('/api/customer/conversations/' + encodeURIComponent(threadId), {
      method: 'DELETE',
      headers: { 'Authorization': 'Bearer ' + s.token }
    }).then(function (resp) {
      return resp.json().then(function (body) { return { ok: resp.ok, body: body }; })
        .catch(function () { return { ok: resp.ok, body: null }; });
    }).then(function (res) {
      if (!res.ok || !res.body || res.body.code !== 0) {
        render();
        notify((res.body && res.body.msg) || '删除失败');
        return;
      }
      state.list = state.list.filter(function (x) { return x.thread_id !== threadId; });
      render();
    }).catch(function () {
      render();
      notify('删除失败，请稍后再试');
    });
  }

  function setKeyword(kw) {
    state.keyword = kw || '';
    render();
  }

  function closeMenu() {
    if (!state.openMenu) return false;
    state.openMenu = null;
    render();
    return true;
  }

  global.ConvList = {
    init: function (options) { opts = options; },
    load: load,
    render: render,
    create: create,
    setKeyword: setKeyword,
    closeMenu: closeMenu,
    state: state
  };
})(window);
