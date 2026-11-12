(() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let csrf = '';
  async function request(path, method = 'GET', data) {
    const response = await fetch(path, {method, headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, ...(data ? {body:JSON.stringify(data)} : {})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败');
    return result;
  }
  function error(node, value) { node.textContent = value instanceof Error ? value.message : value; }
  function dialog(title) {
    const node = document.createElement('dialog');
    node.className = 'accounts-dialog';
    node.innerHTML = `<header><h2>${title}</h2><button type="button" class="quiet-button" data-close>关闭</button></header><p role="alert" class="account-error"></p><div data-content></div>`;
    node.querySelector('[data-close]').onclick = () => node.close();
    node.addEventListener('close', () => node.remove());
    document.body.append(node); node.showModal(); return node;
  }
  async function showAccount(info) {
    const node = dialog('账号设置'), content = node.querySelector('[data-content]'), problem = node.querySelector('[role=alert]');
    content.innerHTML = `<p>${escape(window.fastlabAccount.name)} · ${escape(window.fastlabAccount.email)}</p><p>${window.fastlabAccount.role === 'admin' ? '管理员' : '用户'}</p>`;
    if (!info.fastcas) return;
    try {
      const status = await request('/api/auth/fastcas/status');
      content.insertAdjacentHTML('beforeend', `<p>${status.link ? '绑定状态：'+escape(status.link.state) : '尚未绑定 FastCAS'}</p><form><label>本地密码<input name="password" type="password" autocomplete="current-password" required></label><button class="primary-button">${status.link ? '解除认证' : '绑定 FastCAS'}</button></form>${status.link?.state === 'prepared' ? '<button data-reconcile>重试认证</button>' : ''}<p>FastCAS 管理员认证后会自动获得平台管理员权限。</p>`);
      content.querySelector('form').onsubmit = async event => {
        event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
        try { const result = await request('/api/auth/fastcas/'+(status.link ? 'revoke' : 'link'), 'POST', Object.fromEntries(new FormData(event.target))); if (result.url) location.assign(result.url); else location.reload(); }
        catch (e) { error(problem,e); button.disabled=false; }
      };
      const retry = content.querySelector('[data-reconcile]');
      if (retry) retry.onclick = async () => { try { await request('/api/auth/fastcas/reconcile','POST',{}); node.close(); await showAccount(info); } catch(e) { error(problem,e); } };
    } catch (e) { error(problem,e); }
  }
  async function showUsers() {
    const node = dialog('用户管理'), content = node.querySelector('[data-content]'), problem = node.querySelector('[role=alert]');
    let users = [], selected = null;
    async function load() {
      users = await request('/api/admin/users');
      render();
    }
    function render() {
      content.innerHTML = `<label>搜索用户<input data-search placeholder="姓名或邮箱"></label><button class="quiet-button" data-new>创建用户</button><div style="overflow-x:auto"><table><thead><tr><th>姓名</th><th>邮箱</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody></table></div><div data-form></div>`;
      const rows = query => {
        content.querySelector('tbody').innerHTML = users.filter(u => `${u.name} ${u.email}`.toLowerCase().includes(query.toLowerCase())).map(u => `<tr><td>${escape(u.name)}</td><td>${escape(u.email)}</td><td>${u.role==='admin'?'管理员':'用户'}</td><td>${u.status==='active'?'启用':'停用'}</td><td><button data-edit="${escape(u.id)}">编辑</button></td></tr>`).join('');
        content.querySelectorAll('[data-edit]').forEach(button => button.onclick = () => { selected=users.find(u=>u.id===button.dataset.edit); form(); });
      };
      rows(''); content.querySelector('[data-search]').oninput = event => rows(event.target.value);
      content.querySelector('[data-new]').onclick = () => { selected=null; form(); };
      form();
    }
    function form() {
      const u = selected || {email:'',name:'',role:'user',status:'active'};
      content.querySelector('[data-form]').innerHTML = `<form class="account-form"><h3>${selected?'编辑用户':'创建用户'}</h3><label>邮箱<input name="email" type="email" required value="${escape(u.email)}"></label><label>姓名<input name="name" maxlength="120" value="${escape(u.name)}"></label><label>${selected?'重置密码（留空保持）':'初始密码'}<input name="password" type="password" autocomplete="new-password" minlength="12" maxlength="256" ${selected?'':'required'}></label><label>角色<select name="role"><option value="user">用户</option><option value="admin" ${u.role==='admin'?'selected':''}>管理员</option></select></label><label>状态<select name="status"><option value="active">启用</option><option value="disabled" ${u.status==='disabled'?'selected':''}>停用</option></select></label><label>操作原因<input name="reason" required maxlength="500"></label><div class="actions"><button type="submit" class="primary-button">保存</button>${selected&&selected.id!==window.fastlabAccount.id?'<button type="button" data-delete>删除用户</button>':''}</div></form>`;
      const element = content.querySelector('form');
      element.onsubmit = async event => { event.preventDefault(); await save(false); };
      const remove = content.querySelector('[data-delete]');
      if (remove) remove.onclick = async () => { if (element.reportValidity() && confirm(`确认删除 ${u.email}？此账号的现有会话将失效。`)) await save(true); };
      async function save(remove) {
        const data = Object.fromEntries(new FormData(element));
        element.querySelectorAll('button').forEach(b=>b.disabled=true);
        try { await request('/api/admin/users'+(selected?'/'+selected.id:''),remove?'DELETE':selected?'PUT':'POST',data); selected=null; await load(); }
        catch(e) { error(problem,e); element.querySelectorAll('button').forEach(b=>b.disabled=false); }
      }
    }
    try { await load(); } catch(e) { error(problem,e); }
  }
  async function loginScreen(info, registering) {
    document.querySelector('.app-shell').hidden = true;
    const node = document.createElement('main'); node.className='accounts-login'; document.body.append(node);
    node.innerHTML = `<h1>${registering?'完善 FastLabs 账号':'登录 FastLabs'}</h1><p role="alert" class="account-error"></p><form class="account-form">${registering?'<label>姓名<input name="name" maxlength="120"></label>':''}<label>邮箱<input name="email" type="email" required autocomplete="username"></label><label>${registering?'本地密码（至少 12 个字符）':'密码'}<input name="password" type="password" required ${registering?'minlength="12"':''} maxlength="256" autocomplete="${registering?'new-password':'current-password'}"></label><button class="primary-button">${registering?'创建并绑定账号':'登录'}</button></form>${info.fastcas?'<p><a href="/api/auth/fastcas/login">使用 FastCAS 登录</a></p>':''}${registering?'<button data-existing class="quiet-button">已有账号，返回登录后绑定</button>':''}`;
    const problem=node.querySelector('[role=alert]'), form=node.querySelector('form');
    if (new URLSearchParams(location.search).get('fastcas')==='failed') error(problem,'FastCAS 登录未完成，请重试或使用本地账号登录。');
    if (registering) {
      try { const profile=await request('/api/auth/fastcas/registration'); form.elements.email.value=profile.email; form.elements.name.value=profile.name; } catch(e) { error(problem,e); }
      node.querySelector('[data-existing]').onclick=()=>{node.remove(); history.replaceState(null,'','/'); void loginScreen(info,false);};
    }
    form.onsubmit=async event=>{
      event.preventDefault(); form.querySelector('button').disabled=true;
      try {
        csrf=(await request('/api/auth/session-init')).csrf;
        const result=await request(registering?'/api/auth/fastcas/registration':'/api/auth/login','POST',Object.fromEntries(new FormData(form)));
        location.assign(result.url || '/');
      } catch(e) { error(problem,e); form.querySelector('button').disabled=false; }
    };
  }
  window.fastlabAccountsReady = (async () => {
    const info=await request('/api/auth/info'); if (!info.enabled) return true;
    if (new URLSearchParams(location.search).get('fastcas')==='register') { await loginScreen(info,true); return false; }
    try { window.fastlabAccount=await request('/api/auth/me'); csrf=window.fastlabAccount.csrf; }
    catch { await loginScreen(info,false); return false; }
    if (window.fastlabAccount.role !== 'admin') {
      document.querySelectorAll('[data-action="settings"]').forEach(button => { button.hidden = true; });
    }
    const controls=document.createElement('div'); controls.className='account-controls';
    controls.innerHTML=`<button data-account>账号设置</button>${window.fastlabAccount.role==='admin'?'<button data-users>用户管理</button>':''}<button data-logout>退出登录</button>`;
    document.querySelector('.sidebar').append(controls);
    controls.querySelector('[data-account]').onclick=()=>void showAccount(info);
    if (controls.querySelector('[data-users]')) controls.querySelector('[data-users]').onclick=()=>void showUsers();
    controls.querySelector('[data-logout]').onclick=async()=>{await request('/api/auth/logout','POST',{});location.assign('/');};
    return true;
  })().catch(e => { document.querySelector('#main-content').textContent=e.message; return false; });
})();
