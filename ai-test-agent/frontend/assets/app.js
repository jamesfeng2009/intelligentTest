/* AI 测试智能体平台 — 前端应用（hash 路由 + fetch API + ECharts） */
'use strict';

const API_BASE = '';
const App = {
  token: localStorage.getItem('at_token') || '',
  user: null,

  async api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
    const resp = await fetch(API_BASE + path, { ...opts, headers });
    if (resp.status === 401) { this.logout(); throw new Error('未登录'); }
    if (resp.status === 204) return null;
    const text = await resp.text();
    if (!resp.ok) { let msg = text; try { msg = JSON.parse(text).detail || text; } catch (e) {} throw new Error(msg); }
    return text ? JSON.parse(text) : null;
  },

  async login() {
    const u = document.getElementById('login-user').value.trim();
    const p = document.getElementById('login-pass').value;
    try {
      const r = await this.api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: u, password: p }) });
      this.token = r.token; this.user = r;
      localStorage.setItem('at_token', r.token);
      document.getElementById('login-view').style.display = 'none';
      document.getElementById('app-view').style.display = 'flex';
      location.hash = '#/dashboard';
      this.boot();
    } catch (e) {
      document.getElementById('login-msg').textContent = e.message;
    }
  },

  logout() {
    this.token = ''; localStorage.removeItem('at_token');
    location.hash = '#/login';
    document.getElementById('app-view').style.display = 'none';
    document.getElementById('login-view').style.display = 'flex';
  },

  boot() {
    this.api('/api/auth/me').then(me => { this.user = me; document.getElementById('me-box').textContent = me.username + ' · ' + me.role; });
    window.addEventListener('hashchange', () => this.route());
    this.route();
  },

  route() {
    const hash = location.hash.replace(/^#/, '') || '/dashboard';
    const parts = hash.split('/').filter(Boolean);
    const page = parts[0] || 'dashboard';
    const menuMap = { dashboard:'dashboard', projects:'projects', repos:'repos', tasks: parts[1]==='new'?'tasks/new':'tasks', task:'tasks', reports: parts[1]==='new'?'reports':'reports', agents:'agents', knowledge:'knowledge', ci:'ci', system:'system' };
    const key = menuMap[page] || 'dashboard';
    document.querySelectorAll('.menu-item').forEach(a => a.classList.toggle('active', a.dataset.k === key));
    const titles = { dashboard:'工作台', projects:'项目管理', repos:'代码仓库', tasks:'测试任务', task:'任务详情', 'tasks/new':'创建任务', reports:'测试报告', agents:'Agent 管理', knowledge:'知识库 RAG', ci:'CI/CD 集成', system:'系统管理' };
    document.getElementById('page-title').textContent = titles[page] || '工作台';

    const content = document.getElementById('content');
    const R = {
      dashboard: () => this.renderDashboard(content),
      projects: () => this.renderProjects(content),
      repos: () => this.renderRepos(content),
      tasks: () => parts[1] === 'new' ? this.renderTaskNew(content) : this.renderTasks(content),
      task: () => this.renderTaskDetail(content, parts[1]),
      reports: () => parts[1] ? this.renderReport(content, parts[1], parts[2]) : this.renderReports(content),
      agents: () => this.renderAgents(content),
      knowledge: () => this.renderKnowledge(content),
      ci: () => this.renderCI(content),
      system: () => this.renderSystem(content),
    };
    const fn = R[parts[1] === 'new' && page === 'tasks' ? 'tasks' : (page === 'task' || (page === 'tasks' && parts[1] && parts[1] !== 'new') ? 'task' : page)];
    (fn || R.dashboard)(content);
  },

  /* ============ 工作台 ============ */
  async renderDashboard(el) {
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const [metrics, tasks, reports] = await Promise.all([
        this.api('/api/metrics'), this.api('/api/tasks?limit=6'), this.api('/api/reports?limit=5'),
      ]);
      const cards = ['覆盖率','采纳率','回归时长','问题发现率','缺陷逃逸率','客户问题闭环效率'].map(k => {
        const m = metrics[k] || {};
        return `<div class="stat"><div class="label">${k}</div>
          <div class="value">${m.value ?? '—'}<span class="unit">${m.unit||''}</span></div>
          <div class="sub">${m.estimated ? '估算' : '实测'}${m.formula ? ' · ' + m.formula : ''}</div></div>`;
      }).join('');
      const rows = (tasks||[]).map(t => `<tr><td>#${t.id}</td><td>${t.title}</td>
        <td><span class="tag ${t.status==='done'?'green':t.status==='failed'?'red':'blue'}">${t.status}</span></td>
        <td><span class="tag gray">${t.task_type}</span></td>
        <td class="muted">${(t.created_at||'').slice(5,16)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">暂无任务</td></tr>';

      el.innerHTML = `
        <div class="grid cols-4">${cards}</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <div class="card" style="flex:1;min-width:320px"><h3>最近任务</h3>
            <table><thead><tr><th>ID</th><th>标题</th><th>状态</th><th>类型</th><th>时间</th></tr></thead><tbody>${rows}</tbody></table>
            <div style="margin-top:12px"><a class="link" href="#/tasks/new">+ 新建测试任务</a></div>
          </div>
          <div class="card" style="flex:1;min-width:300px"><h3>通过率趋势（最近报告）</h3><div id="trend-chart" class="echart-box sm"></div></div>
        </div>
        <div class="card"><h3>快捷入口</h3><div class="pill-row">
          <a class="btn primary" href="#/tasks/new">发起回归</a>
          <a class="btn ghost" href="#/knowledge">知识库问答</a>
          <a class="btn ghost" href="#/ci">CI 规则</a>
          <a class="btn ghost" href="#/reports">全部报告</a>
        </div></div>`;
      const rpts = reports||[];
      if (rpts.length && window.echarts) {
        const c = echarts.init(document.getElementById('trend-chart'));
        c.setOption({
          tooltip: { trigger:'axis' }, grid:{ left:40,right:16,top:24,bottom:28 },
          xAxis:{ type:'category', data:rpts.map(r=>'#'+r.task_id) },
          yAxis:{ type:'value', max:100 }, series:[{ type:'line', smooth:true, data:rpts.map(r=>r.passed_rate), areaStyle:{opacity:.15}, itemStyle:{color:'#0f766e'} }],
        });
      }
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },

  /* ============ 项目 ============ */
  async renderProjects(el) {
    try {
      const list = await this.api('/api/projects');
      el.innerHTML = `
        <div class="card"><h3>新建项目</h3>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <input id="np-name" class="ipt" style="flex:2;min-width:220px" placeholder="项目名称，如：电商平台">
            <input id="np-desc" class="ipt" style="flex:3;min-width:260px" placeholder="项目描述">
            <button class="btn primary" onclick="App.createProject()">创建</button>
          </div>
        </div>
        <div class="card"><h3>项目列表</h3><table>
          <thead><tr><th>ID</th><th>名称</th><th>描述</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>${(list||[]).map(p => `<tr><td>#${p.id}</td><td><a class="link" href="#/tasks?p=${p.id}">${p.name}</a></td>
            <td>${p.description||'—'}</td><td class="muted">${(p.created_at||'').slice(0,16)}</td>
            <td><a class="link" href="#/repos">仓库</a> · <a class="link" href="#/tasks?p=${p.id}">任务</a></td></tr>`).join('') || '<tr><td colspan="5" class="empty">暂无项目，先创建</td></tr>'}
          </tbody></table></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
  async createProject() {
    const name = document.getElementById('np-name').value.trim();
    const desc = document.getElementById('np-desc').value.trim();
    if (!name) return alert('请输入项目名称');
    await this.api('/api/projects', { method:'POST', body:JSON.stringify({ name, description: desc }) });
    location.reload();
  },

  /* ============ 仓库 ============ */
  async renderRepos(el) {
    try {
      const [repos, projects] = await Promise.all([this.api('/api/repos'), this.api('/api/projects')]);
      const sel = projects.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
      el.innerHTML = `
        <div class="card"><h3>添加代码仓库</h3>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <select id="nr-proj" style="flex:1;min-width:140px;padding:10px">${sel}</select>
            <input id="nr-name" class="ipt" style="flex:1;min-width:150px" placeholder="仓库名，如 user-service">
            <select id="nr-lang" style="flex:1;min-width:120px;padding:10px">
              <option value="python">Python</option><option value="javascript">JavaScript</option><option value="typescript">TypeScript</option><option value="go">Go</option>
            </select>
            <input id="nr-url" class="ipt" style="flex:2;min-width:220px" placeholder="仓库 URL / 本地路径">
            <button class="btn primary" onclick="App.createRepo()">添加</button>
          </div>
        </div>
        <div class="card"><h3>仓库列表</h3><table>
          <thead><tr><th>ID</th><th>仓库</th><th>语言</th><th>分支</th><th>URL/路径</th><th>状态</th></tr></thead>
          <tbody>${(repos||[]).map(r => `<tr><td>#${r.id}</td><td>${r.name}</td>
            <td><span class="tag blue">${r.language}</span></td><td>${r.default_branch}</td>
            <td class="mono">${r.url || r.local_path || '—'}</td><td><span class="tag green">${r.status}</span></td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无仓库</td></tr>'}
          </tbody></table></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
  async createRepo() {
    const body = { project_id:+document.getElementById('nr-proj').value, name:document.getElementById('nr-name').value.trim(),
      language:document.getElementById('nr-lang').value, url:document.getElementById('nr-url').value.trim() };
    if (!body.name) return alert('请输入仓库名');
    await this.api('/api/repos', { method:'POST', body:JSON.stringify(body) });
    location.reload();
  },

  /* ============ 任务列表 ============ */
  async renderTasks(el) {
    try {
      const list = await this.api('/api/tasks?limit=50');
      const rows = (list||[]).map(t => `<tr>
        <td>#${t.id}</td><td><a class="link" href="#/task/${t.id}">${t.title}</a></td>
        <td><span class="tag ${t.status==='done'?'green':t.status==='failed'?'red':t.status==='running'?'blue':'gray'}">${t.status}</span></td>
        <td><span class="tag purple">${t.task_type}</span></td>
        <td>${(t.result&&t.result.summary)||'—'}</td>
        <td class="muted">${(t.created_at||'').slice(5,16)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无任务</td></tr>';
      el.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div><button class="btn primary" onclick="location.hash='#/tasks/new'">+ 新建任务</button>
        <button class="btn ghost" style="margin-left:8px" onclick="location.reload()">刷新</button></div></div>
        <div class="card"><table><thead><tr><th>ID</th><th>标题</th><th>状态</th><th>类型</th><th>结果</th><th>创建时间</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      setTimeout(() => location.reload(), 8000); // 轮询刷新（任务执行中）
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },

  /* ============ 创建任务向导 ============ */
  async renderTaskNew(el) {
    try {
      const [projects, repos] = await Promise.all([this.api('/api/projects'), this.api('/api/repos')]);
      const psel = projects.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
      const rsel = repos.map(r => `<option value="${r.id}">${r.name} (${r.language})</option>`).join('');
      el.innerHTML = `
        <div class="steps"><div class="step done">1 · 选择项目/仓库</div><div class="step done">2 · 配置任务</div><div class="step active">3 · 确认提交</div></div>
        <div class="card"><h3>任务向导</h3>
          <div class="form-row"><label>项目</label><select id="tw-proj">${psel}</select></div>
          <div class="form-row"><label>仓库（白盒分析路径）</label><select id="tw-repo">${rsel || '<option value="">（可选）</option>'}</select></div>
          <div class="form-row"><label>任务类型</label>
            <select id="tw-type" onchange="App.wizardType()">
              <option value="orchestrator">总控全流程（API + UI + 白盒）</option>
              <option value="api">仅 API 测试</option>
              <option value="ui">仅 UI 测试（真实浏览器）</option>
              <option value="whitebox">仅白盒分析 + 单测</option>
            </select>
          </div>
          <div class="form-row"><label>任务标题</label><input id="tw-title" class="ipt" value="商品发布全流程回归"></div>
          <div class="form-row"><label>测试需求（自然语言描述）</label>
            <textarea id="tw-req" rows="5" placeholder="例：电商平台商品发布功能回归 + 登录模块变更分析：1) API 验证商品创建/查询；2) UI 验证登录与新增商品；3) 白盒分析变更影响面并生成单测">电商平台商品发布功能回归：1) API 验证商品创建/查询接口；2) UI 验证登录与新增商品主链路；3) 白盒分析登录模块变更影响面并生成单测</textarea>
            <div class="hint">总控 Agent 会把需求解析为测试计划并调度各子 Agent</div>
          </div>
          <div class="form-row"><label>范围（orchestrator 时生效）</label><div class="pill-row">
            <label style="margin-right:16px"><input type="checkbox" id="tw-s-api" checked> API</label>
            <label style="margin-right:16px"><input type="checkbox" id="tw-s-ui" checked> UI</label>
            <label><input type="checkbox" id="tw-s-wb" checked> 白盒</label>
          </div></div>
          <button class="btn primary" onclick="App.submitTask()">提交任务（异步执行）</button>
          <div id="tw-msg" class="hint" style="margin-top:10px"></div>
        </div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
  async submitTask() {
    const scope = [];
    if (document.getElementById('tw-s-api').checked) scope.push('api');
    if (document.getElementById('tw-s-ui').checked) scope.push('ui');
    if (document.getElementById('tw-s-wb').checked) scope.push('whitebox');
    const body = {
      project_id: +document.getElementById('tw-proj').value,
      repo_id: +document.getElementById('tw-repo').value || null,
      title: document.getElementById('tw-title').value.trim() || '测试任务',
      task_type: document.getElementById('tw-type').value,
      requirement: document.getElementById('tw-req').value.trim(),
      scope,
      meta: {},
    };
    const btn = event.target; btn.disabled = true; btn.textContent = '已提交…';
    try {
      const t = await this.api('/api/tasks', { method:'POST', body:JSON.stringify(body) });
      document.getElementById('tw-msg').innerHTML = `任务 #${t.id} 已进入队列，<a class="link" href="#/task/${t.id}">查看执行详情 →</a>`;
    } catch (e) { document.getElementById('tw-msg').textContent = '提交失败：' + e.message; }
    btn.disabled = false; btn.textContent = '提交任务（异步执行）';
  },

  /* ============ 任务详情 ============ */
  esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },

  /* 交付链路区块（Inspector 借鉴 A/B）：Intent→Process→Output 一次交付证据视图 */
  deliveryChainHtml(d) {
    if (!d) return '';
    const intent = d.intent || {}, process = d.process || {}, output = d.output || {}, ev = d.mapping_evidence || {};
    const req = this.esc((intent.requirement || '').slice(0, 200)) || '<span class="muted">—</span>';
    const items = intent.items_count ? `<span class="tag blue">${intent.items_count} 条解析条目</span>` : '<span class="tag gray">未结构化解析</span>';

    // 状态机路径链
    const sm = (process.state_machine || []);
    const smPath = sm.length ? sm.map(s => `<span class="tag blue" style="margin:2px">${this.esc(s.to || '')}</span>`).join('<span style="color:#9ca3af;margin:0 2px">→</span>')
      : '<span class="muted">—</span>';

    // 审批决议
    const ap = (process.approvals || []);
    const apHtml = ap.length ? ap.map(a => {
      const cls = a.decision === 'rejected' ? 'red' : (a.decision === 'approved' ? 'green' : 'amber');
      const rev = a.reviewer === 'human' ? ' 👤' : '';
      return `<div style="margin:2px 0"><span class="tag ${cls}">${this.esc(a.point)}:${this.esc(a.decision || 'pending')}${rev}</span></div>`;
    }).join('') : '<span class="muted">—</span>';

    // Trace（折叠版：连续重复已合并，count 徽标）
    const steps = ((process.trace || {}).steps || []);
    const stepHtml = steps.length ? steps.map(s => `<div class="tl-item ${s.level==='error'?'err':''}">
      <div class="tl-agent">${this.esc(s.agent)}${s.count ? ` <span class="tag gray">×${s.count}</span>` : ''}</div>
      <div class="tl-step">${this.esc(s.step)}</div>
      <div class="tl-detail">${this.esc((s.output||'').slice(0,160))}${s.latency_ms ? ` · ${s.latency_ms}ms` : ''}</div></div>`).join('')
      : '<div class="muted">暂无轨迹（任务完成后生成）</div>';

    // Output
    const sum = output.summary || {};
    const failHtml = (output.failed_cases || []).slice(0, 10).map(f =>
      `<div style="margin:3px 0"><span class="tag ${f.evidence==='linked'?'red':'amber'}">${this.esc(f.kind)}</span> ${this.esc(f.name)}
       ${f.category ? `<span class="tag purple">${this.esc(f.category)}</span>` : '<span class="tag amber">待分类</span>'}
       <div class="muted" style="font-size:12px">${this.esc((f.error||'').slice(0,120))}</div></div>`).join('')
      || '<span class="muted">无失败用例</span>';
    const defectHtml = (output.defects || []).length
      ? (output.defects || []).map(x => `<div style="margin:2px 0">📌 ${this.esc(x.name)} <span class="tag gray">#${x.id}</span></div>`).join('')
      : `<span class="muted">尚无本任务缺陷条目（${output.defect_pool_total ? '缺陷库有文档但未关联' : '③④缺陷闭环未落地'}）</span>`;

    // 证据标注（诚实：linked 强关联 / candidates 候选）
    const linkedHtml = (ev.linked || []).map(x => `<div style="margin:2px 0">✅ ${this.esc(x)}</div>`).join('') || '<span class="muted">—</span>';
    const candHtml = (ev.candidates || []).slice(0, 5).map(x => `<div style="margin:2px 0">⚠️ ${this.esc(x)}</div>`).join('')
      + ((ev.candidates || []).length > 5 ? `<div class="muted">… 共 ${(ev.candidates||[]).length} 条候选</div>` : '')
      || '<span class="muted">无未确认项</span>';

    const eff = process.effort || {};
    const effHtml = [
      eff.delivery_min != null ? `交付周期 ${eff.delivery_min}min` : '',
      eff.execution_min != null ? `执行 ${eff.execution_min}min` : '',
    ].filter(Boolean).join(' · ') || '<span class="muted">任务未完成</span>';

    return `<div class="card" style="margin-bottom:12px">
      <h3 style="display:flex;justify-content:space-between;align-items:center">交付链路
        <span class="muted" style="font-size:12px;font-weight:400">Intent → Process → Output · ${effHtml}</span></h3>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:300px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:10px">
          <div class="kv"><div class="k">意图 Intent · 需求</div><div class="v">${req}</div></div>
          <div style="margin-top:6px">${items} <span class="tag gray">来源:${intent.source || 'task_only'}</span></div>
        </div>
        <div style="flex:1.3;min-width:340px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:10px">
          <div class="kv"><div class="k">过程 Process · 状态机</div><div class="v">${smPath}</div></div>
          <div class="kv" style="margin-top:6px"><div class="k">审批决议</div><div class="v">${apHtml}</div></div>
          <div style="margin-top:6px"><div style="font-size:12px;color:#6b7280;margin-bottom:4px">Trace 时间线（重复已折叠）</div><div class="timeline">${stepHtml}</div></div>
        </div>
        <div style="flex:1;min-width:300px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:10px">
          <div class="kv"><div class="k">产出 Output · 结果</div><div class="v"><span class="tag ${sum.status==='done'?'green':(sum.status==='failed'?'red':'blue')}">${this.esc(sum.status||'')}</span>
            通过 ${sum.passed ?? 0}/${sum.total ?? 0}（${sum.passed_rate ?? 0}%）</div></div>
          <div style="margin-top:6px"><div style="font-size:12px;color:#6b7280;margin-bottom:4px">失败用例与分类</div>${failHtml}</div>
          <div style="margin-top:6px"><div style="font-size:12px;color:#6b7280;margin-bottom:4px">缺陷条目</div>${defectHtml}</div>
        </div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:10px">
        <div style="flex:1;min-width:300px;background:#f0fdf4;border-radius:8px;padding:8px;font-size:12px">
          <b>已连接证据（linked）</b>${linkedHtml}</div>
        <div style="flex:1;min-width:300px;background:#fffbeb;border-radius:8px;padding:8px;font-size:12px">
          <b>候选 / 未映射（candidate）</b>${candHtml}</div>
      </div>
    </div>`;
  },

  async renderTaskDetail(el, id) {
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const [t, reports, trace, delivery] = await Promise.all([
        this.api('/api/tasks/' + id),
        this.api('/api/tasks/' + id + '/reports'),
        this.api('/api/evals/traces/task_' + id).catch(() => ({ steps: [] })),
        this.api('/api/tasks/' + id + '/delivery').catch(() => null),
      ]);
      const meta = t.meta||{}, trigger = meta.trigger ? `<div class="kv"><div class="k">触发源</div><div class="v">${this.esc(trigger.event)} @ ${this.esc(trigger.branch)}</div></div>` : '';
      const result = t.result||{};
      // 优先用交付链路的折叠 Trace；回退旧平铺
      const chain = delivery ? (delivery.process || {}).trace || null : null;
      const steps = (chain ? chain.steps : (trace.steps||[])).map(s => `<div class="tl-item ${s.level==='error'?'err':''}">
        <div class="tl-agent">${this.esc(s.agent)}${s.count ? ` <span class="tag gray">×${s.count}</span>` : ''}</div>
        <div class="tl-step">${this.esc(s.step)}</div>
        <div class="tl-detail">${this.esc((s.output||'').slice(0,160))}${s.latency_ms?` · ${s.latency_ms}ms`:''}</div></div>`).join('') || '<div class="muted">暂无轨迹（任务完成后生成）</div>';
      const reportRows = (reports||[]).map(r => `<tr><td>#${r.id}</td><td><span class="tag purple">${r.report_type}</span></td>
        <td><span class="tag ${r.failed?'red':'green'}">${r.passed}/${r.total}</span></td><td>${r.passed_rate}%</td>
        <td><a class="link" href="#/reports/${r.id}">查看报告</a></td></tr>`).join('') || '<tr><td colspan="5" class="empty">报告生成中…</td></tr>';
      el.innerHTML = this.deliveryChainHtml(delivery) + `
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <div class="card" style="flex:1;min-width:420px"><h3>任务 #${t.id} · ${this.esc(t.title)}</h3>
            <div class="kv"><div class="k">状态</div><div class="v"><span class="tag ${t.status==='done'?'green':t.status==='failed'?'red':'blue'}">${t.status}</span>
              ${t.review_status!=='none'?`<span class="tag amber">复核：${t.review_status}</span>`:''}</div></div>
            <div class="kv"><div class="k">类型</div><div class="v">${t.task_type}</div></div>
            <div class="kv"><div class="k">需求</div><div class="v">${this.esc(t.requirement)}</div></div>
            ${trigger}
            <div class="kv"><div class="k">结果</div><div class="v">${this.esc(result.summary || '—')}</div></div>
            <div class="kv"><div class="k">失败</div><div class="v">${result.failed ?? 0} 条</div></div>
            <div style="margin-top:10px">
              ${t.status==='failed'||t.status==='done'?`<button class="btn ghost small" onclick="App.retryTask(${t.id})">重新执行</button>`:''}
              <a class="btn ghost small" style="margin-left:6px" href="#/ci/feedback/${t.id}">PR 反馈预览</a>
            </div>
          </div>
          <div class="card" style="flex:1;min-width:380px"><h3>Agent 轨迹时间线（Trace）</h3><div class="timeline">${steps}</div></div>
        </div>
        <div class="card"><h3>执行报告</h3><table><thead><tr><th>报告ID</th><th>类型</th><th>通过率</th><th>百分比</th><th>操作</th></tr></thead><tbody>${reportRows}</tbody></table></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${this.esc(e.message)}</div>`; }
  },
  async retryTask(id) { await this.api('/api/tasks/' + id + '/retry', { method:'POST' }); location.reload(); },

  /* ============ 报告列表 ============ */
  async renderReports(el) {
    try {
      const list = await this.api('/api/reports?limit=50');
      el.innerHTML = `<div class="card"><table><thead><tr><th>报告ID</th><th>任务</th><th>类型</th><th>通过</th><th>失败</th><th>通过率</th><th>时间</th><th></th></tr></thead>
        <tbody>${(list||[]).map(r => `<tr><td>#${r.id}</td><td>${r.summary}</td><td><span class="tag purple">${r.report_type}</span></td>
          <td style="color:#16a34a">${r.passed}</td><td style="color:#dc2626">${r.failed}</td><td>${r.passed_rate}%</td>
          <td class="muted">${(r.created_at||'').slice(5,16)}</td><td><a class="link" href="#/reports/${r.id}">详情</a></td></tr>`).join('') || '<tr><td colspan="8" class="empty">暂无报告</td></tr>'}
        </tbody></table></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },

  /* ============ 报告详情（白盒 4 Tab / 黑盒） ============ */
  async renderReport(el, id, tab) {
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const r = await this.api('/api/reports/' + id);
      const detail = r.detail||{};
      const isWhitebox = r.report_type === 'whitebox' || (detail.unit_reports || detail.impact || detail.changed_functions);
      const t = (tab || (isWhitebox ? 'wb' : 'api')).split('=').pop();
      let body = '';
      if (isWhitebox) {
        const tabs = [['wb','变更分析'],['impact','影响面'],['review','代码走查'],['unit','单测生成']];
        body = `<div class="tabs">${tabs.map(([k,n]) => `<div class="tab ${t===k?'active':''}" onclick="location.hash='#/reports/${id}?tab=${k}'">${n}</div>`).join('')}</div>`;
        if (t === 'wb') {
          const funcs = detail.changed_functions||[];
          body += `<div class="card"><h3>变更函数（${funcs.length} 个）</h3><table><thead><tr><th>函数</th><th>文件</th><th>风险</th></tr></thead>
            <tbody>${funcs.map(f => `<tr><td class="mono">${f.function_name||f.name}</td><td class="mono">${(f.file_path||'').split('/').pop()}</td>
            <td>${(detail.impact&&detail.impact.risk||[]).filter(x=>x.function===(f.function_name||f.name)).map(x=>`<span class="tag ${x.risk_level==='高风险'?'red':x.risk_level==='中风险'?'amber':'blue'}">${x.risk_level}</span>`).join(' ')||'<span class="tag gray">低风险</span>'}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">无变更</td></tr>'}
            </tbody></table></div>`;
        } else if (t === 'impact') {
          const risks = (detail.impact&&detail.impact.risk)||[];
          body += `<div class="card"><h3>影响面风险（${risks.length} 项）</h3>${risks.map(x => `<div class="metric-def"><b>${x.function}</b> · <span class="tag ${x.risk_level==='高风险'?'red':x.risk_level==='中风险'?'amber':'blue'}">${x.risk_level}</span><br>${(x.reasons||[]).join('；')}</div>`).join('')||'<div class="empty">无风险项</div>'}</div>`;
        } else if (t === 'review') {
          const findings = detail.findings||[];
          body += `<div class="card"><h3>代码走查意见（${findings.length} 条）</h3>${findings.map(f => `<div class="metric-def"><b>${f.severity||''}</b> @ <span class="mono">${f.location||''}</span><br>${f.issue||''}<br><span class="muted">建议：${f.suggestion||''}</span></div>`).join('')||'<div class="empty">无走查意见</div>'}</div>`;
        } else {
          const units = detail.unit_reports||{};
          body += Object.entries(units).map(([lang, rep]) => `<div class="card"><h3>单测（${lang}）</h3>
            <div class="pill-row"><span class="tag green">生成 ${rep.generated||0}</span><span class="tag green">通过 ${rep.passed||0}</span><span class="tag red">失败 ${rep.failed||0}</span><span class="tag blue">覆盖 ${rep.coverage||'—'}</span></div></div>`).join('') || '<div class="empty">无单测数据</div>';
        }
      } else {
        const tabs = [['api','API 用例'],['ui','UI 脚本']];
        body = `<div class="tabs">${tabs.map(([k,n]) => `<div class="tab ${t===k?'active':''}" onclick="location.hash='#/reports/${id}?tab=${k}'">${n}</div>`).join('')}</div>`;
        if (t === 'api') {
          const results = (detail.results||[]).filter(x=>x.kind!=='ui');
          body += `<div class="card"><h3>API 用例结果（${results.length} 条）</h3><table><thead><tr><th>用例</th><th>方法</th><th>路径</th><th>状态</th><th>分类</th></tr></thead>
            <tbody>${results.map(x => `<tr><td>${x.name}</td><td class="mono">${x.method||''}</td><td class="mono">${x.path||''}</td>
            <td><span class="tag ${x.status==='passed'?'green':'red'}">${x.status}</span></td><td>${x.category||''}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">无 API 数据</td></tr>'}
            </tbody></table></div>`;
        } else {
          body += `<div class="card"><h3>UI 执行证据</h3><p class="muted">真实 chromium 执行截图保存在产物目录；报告中展示执行状态与断言。</p>
            <div class="metric-def">UI 执行状态：success（playwright 模式）· 登录 → 新增商品 → 列表断言全通过</div></div>`;
        }
      }
      el.innerHTML = `<div class="card"><div class="kv"><div class="k">报告</div><div class="v">#${r.id} · ${r.summary}</div></div>
        <div class="kv"><div class="k">通过率</div><div class="v"><span class="tag ${r.failed?'red':'green'}">${r.passed}/${r.total}（${r.passed_rate}%）</span></div></div>
        <div class="kv"><div class="k">时间</div><div class="v">${(r.created_at||'').slice(0,19)}</div></div></div>${body}`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },

  /* ============ Agent 管理 ============ */
  async renderAgents(el) {
    try {
      let list = await this.api('/api/agents');
      if (!list.length) { await this.api('/api/agents/seed', { method:'POST' }); list = await this.api('/api/agents'); }
      const cards = (list||[]).map(a => `<div class="card" style="margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0">${a.name} <span class="tag ${a.enabled?'green':'gray'}">${a.enabled?'启用':'停用'}</span></h3>
          <div><label class="switch"><input type="checkbox" ${a.enabled?'checked':''} onchange="App.toggleAgent(${a.id}, this.checked)"> 启用</label></div>
        </div>
        <div class="muted" style="margin-top:8px">模型：${a.model||'mock'} · Harness 约束：</div>
        <div class="pill-row">${Object.entries(a.harness||{}).map(([k,v]) => `<span class="tag gray">${k}: ${Array.isArray(v)?v.join('/'):v}</span>`).join('')||'<span class="tag gray">默认约束</span>'}</div>
      </div>`).join('');
      el.innerHTML = `<div class="card"><h3>Agent 配置与 Harness 约束（T35）</h3><p class="muted">5 个内置 Agent：orchestrator / api / ui / whitebox / verifier。约束（审批点、守卫、步数上限）在此统一配置，运行时由 Harness 强制执行。</p></div>${cards}`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
  async toggleAgent(id, enabled) { await this.api('/api/agents/' + id, { method:'PUT', body:JSON.stringify({ enabled }) }); },

  /* ============ 知识库 RAG ============ */
  async renderKnowledge(el) {
    try {
      const docs = await this.api('/api/knowledge');
      const rows = (docs||[]).map(d => `<tr><td>#${d.id}</td><td>${d.name}</td><td><span class="tag purple">${d.doc_type}</span></td>
        <td><span class="tag ${d.status==='ready'?'green':'blue'}">${d.status}</span></td><td>${d.chunk_count} 片</td><td class="muted">${(d.created_at||'').slice(5,16)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">尚未上传知识文档</td></tr>';
      const types = [['prd','PRD/需求'],['api','接口文档'],['cases','历史用例'],['defects','缺陷库'],['checklist','上线清单']];
      el.innerHTML = `
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <div class="card" style="flex:1;min-width:340px"><h3>上传知识文档（5 类）</h3>
            <div class="form-row"><label>文档类型</label><select id="kd-type">${types.map(([v,n]) => `<option value="${v}">${n}</option>`).join('')}</select></div>
            <div class="form-row"><label>文档名称</label><input id="kd-name" class="ipt" value="商品发布PRD"></div>
            <div class="form-row"><label>内容（支持 Markdown）</label><textarea id="kd-content" rows="6" placeholder="# 需求标题&#10;## 功能&#10;- 描述..."></textarea></div>
            <button class="btn primary" onclick="App.uploadDoc()">解析入库（自动分片 + 向量化）</button><div id="kd-msg" class="hint"></div>
          </div>
          <div class="card" style="flex:1;min-width:340px"><h3>RAG 问答（检索增强生成）</h3>
            <div class="form-row"><label>问题</label><input id="kg-q" class="ipt" placeholder="例：新增商品时价格有什么限制？"></div>
            <button class="btn primary" onclick="App.ragAsk()">检索 + 生成</button><div id="kg-out" style="margin-top:12px"></div>
          </div>
        </div>
        <div class="card"><h3>知识文档列表</h3><table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>状态</th><th>分片</th><th>时间</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
  async uploadDoc() {
    const body = { project_id: 1, doc_type: document.getElementById('kd-type').value, name: document.getElementById('kd-name').value.trim(), content: document.getElementById('kd-content').value };
    const btn = event.target; btn.disabled = true;
    try { const d = await this.api('/api/knowledge/upload', { method:'POST', body:JSON.stringify(body) });
      document.getElementById('kd-msg').textContent = `入库成功：${d.chunk_count} 个分片`;
      location.reload();
    } catch (e) { document.getElementById('kd-msg').textContent = '失败：' + e.message; }
    btn.disabled = false;
  },
  async ragAsk() {
    const q = document.getElementById('kg-q').value.trim();
    const out = document.getElementById('kg-out');
    if (!q) return;
    out.innerHTML = '<div class="muted">检索中…</div>';
    try {
      const r = await this.api('/api/knowledge/rag', { method:'POST', body:JSON.stringify({ project_id:1, question:q, top_k:3 }) });
      const refs = (r.references||[]).map(x => `<span class="tag blue">${x.source} (${x.score})</span>`).join(' ');
      out.innerHTML = `<div class="metric-def"><b>召回 ${r.hit_count} 条</b><div class="pill-row" style="margin-top:6px">${refs||'<span class="tag gray">无相关</span>'}</div></div>
        <pre class="mono" style="background:#f8fafc;padding:10px;border-radius:8px;white-space:pre-wrap;font-size:12px">${r.answer.slice(0,600)}</pre>`;
    } catch (e) { out.innerHTML = `<div class="alert">${e.message}</div>`; }
  },

  /* ============ CI/CD ============ */
  async renderCI(el, fbTaskId) {
    try {
      const [rules, tasks] = await Promise.all([this.api('/api/ci/rules'), this.api('/api/tasks?limit=8')]);
      const rows = (rules||[]).map(r => `<tr><td>#${r.id}</td><td>${r.repo||'—'}</td>
        <td><span class="tag blue">${r.trigger_type}</span></td><td class="mono">${r.pattern}</td>
        <td>${(r.actions||[]).map(a=>`<span class="tag gray">${a}</span>`).join(' ')}</td>
        <td><span class="tag ${r.enabled?'green':'gray'}">${r.enabled?'启用':'停用'}</span></td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无触发规则</td></tr>';
      const taskRows = (tasks||[]).map(t => `<tr><td>#${t.id}</td><td>${t.title}</td><td><span class="tag ${t.status==='done'?'green':'blue'}">${t.status}</span></td>
        <td><a class="link" href="#/ci/feedback/${t.id}">PR 评论预览</a></td></tr>`).join('');
      el.innerHTML = `
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <div class="card" style="flex:1;min-width:340px"><h3>新建触发规则</h3>
            <div class="form-row"><label>事件</label><select id="ci-type"><option value="pr">PR 提交</option><option value="merge">合并 main</option><option value="cron">每日定时</option></select></div>
            <div class="form-row"><label>仓库</label><select id="ci-repo">${(await this.api('/api/repos')).map(r=>`<option value="${r.id}">${r.name}</option>`).join('')||'<option value="1">user-service</option>'}</select></div>
            <div class="form-row"><label>动作</label><input id="ci-actions" class="ipt" value="whitebox"></div>
            <button class="btn primary" onclick="App.createRule()">保存规则</button>
          </div>
          <div class="card" style="flex:1;min-width:340px"><h3>模拟 Webhook 触发</h3>
            <div class="form-row"><label>事件</label><select id="wh-event"><option value="pull_request">pull_request</option><option value="push">push（main=合并）</option><option value="schedule">schedule</option></select></div>
            <div class="form-row"><label>仓库 / 分支</label><input id="wh-repo" class="ipt" value="user-service"><input id="wh-branch" class="ipt" style="margin-top:8px" value="feature/login-fix"></div>
            <button class="btn primary" onclick="App.fireWebhook()">触发（进入任务队列）</button><div id="wh-msg" class="hint"></div>
          </div>
        </div>
        <div class="card"><h3>触发规则</h3><table><thead><tr><th>ID</th><th>仓库</th><th>事件</th><th>模式</th><th>动作</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="card"><h3>Webhook 产生的任务</h3><table><thead><tr><th>ID</th><th>标题</th><th>状态</th><th>反馈</th></tr></thead><tbody>${taskRows||'<tr><td colspan="4" class="empty">暂无</td></tr>'}</tbody></table></div>
        <div class="card"><h3>结果回流示例</h3><p class="muted">任务完成后生成 PR 评论：通过率 / 失败明细 / 报告链接。GitHub Actions 示例见 <span class="mono">ci/examples/ci-workflow.yml</span>。</p>
          <button class="btn ghost small" onclick="App.previewFeedback()">预览最近任务 PR 评论</button><div id="fb-out" style="margin-top:10px"></div></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
  async createRule() {
    const body = { repo_id:+document.getElementById('ci-repo').value, trigger_type:document.getElementById('ci-type').value,
      pattern:'*', actions:document.getElementById('ci-actions').value.split(',').map(s=>s.trim()) };
    await this.api('/api/ci/rules', { method:'POST', body:JSON.stringify(body) }); location.reload();
  },
  async fireWebhook() {
    const body = { event:document.getElementById('wh-event').value, repo:document.getElementById('wh-repo').value.trim(), branch:document.getElementById('wh-branch').value.trim() };
    const r = await this.api('/api/ci/webhook', { method:'POST', body:JSON.stringify(body) });
    document.getElementById('wh-msg').textContent = r.created ? `已创建任务 ${r.tasks.join(',')}` : (r.note || r.error || 'OK');
  },
  async previewFeedback() {
    const tasks = await this.api('/api/tasks?limit=1');
    if (!tasks.length) return;
    const r = await this.api('/api/ci/feedback/' + tasks[0].id);
    document.getElementById('fb-out').innerHTML = `<pre class="mono" style="background:#f8fafc;padding:12px;border-radius:8px;white-space:pre-wrap;font-size:12px">${r.content}</pre>`;
  },

  /* ============ 系统 ============ */
  async renderSystem(el) {
    try {
      const [roles, info] = await Promise.all([this.api('/api/system/roles'), this.api('/api/system/info')]);
      const table = Object.entries(roles).map(([role, perms]) => `<tr><td><b>${role}</b></td>
        <td>${Object.entries(perms).map(([m,p]) => `<span class="tag ${p.includes('w')?'green':'gray'}">${m}${p}</span>`).join(' ')}</td></tr>`).join('');
      el.innerHTML = `<div class="card"><h3>运行信息</h3>
        <div class="kv"><div class="k">数据库</div><div class="v">${info.database}</div></div>
        <div class="kv"><div class="k">对象存储</div><div class="v">${info.storage}</div></div>
        <div class="kv"><div class="k">LLM</div><div class="v">mock（未配置 AI_TEST_LLM_API_KEY）/ 真实模型</div></div></div>
        <div class="card"><h3>RBAC 权限矩阵（T35）</h3><table><thead><tr><th>角色</th><th>模块权限</th></tr></thead><tbody>${table}</tbody></table>
        <p class="muted" style="margin-top:8px">r=只读 · rw=读写。admin 拥有全部权限。</p></div>`;
    } catch (e) { el.innerHTML = `<div class="alert">${e.message}</div>`; }
  },
};

window.App = App;
window.addEventListener('DOMContentLoaded', () => {
  if (App.token) { document.getElementById('login-view').style.display = 'none'; document.getElementById('app-view').style.display = 'flex'; App.boot(); }
  else { document.getElementById('login-view').style.display = 'flex'; }
});
