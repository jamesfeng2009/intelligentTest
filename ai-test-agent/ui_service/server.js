// AI 测试智能体 — UI 执行服务（Express + Midscene + Playwright）
//
// 独立部署：UI 测试需要浏览器，资源重，与 FastAPI 主后端分离。
// 执行引擎三层降级：
//   1) midscene 可用（且 AI_TEST_UI_USE_MIDSCENE=true，需配视觉 LLM Key）→ AI 语义定位执行（主力）
//   2) 仅 playwright 可用 → 语义化 playwright 定位执行（默认真实浏览器路径，不依赖 LLM）
//   3) 都不可用 → 模拟执行（演示模式，明确标注未实际运行浏览器）
//
// API:
//   POST /api/ui/run   { task_id, test_code, base_url, timeout }
//   GET  /api/health

const express = require('express');
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { promisify } = require('util');
const execFileP = promisify(execFile);

// 延迟加载可选依赖（未安装时降级）
let PlaywrightAgent = null;
let playwright = null;
try {
  playwright = require('playwright');
} catch (e) { playwright = null; }
try {
  const mid = require('@midscene/web');
  PlaywrightAgent = mid.PlaywrightAgent || null;
} catch (e) { PlaywrightAgent = null; }

const app = express();
app.use(express.json({ limit: '2mb' }));

function log(level, msg, extra) {
  console.log(JSON.stringify({ ts: new Date().toISOString(), level, msg, ...(extra || {}) }));
}

function mode() {
  // 允许强制模拟执行（无浏览器 / CI / 演示场景）
  if (process.env.AI_TEST_UI_FORCE_SIMULATED === 'true') return 'simulated';
  // midscene 语义定位：显式开启且需配置视觉 LLM（未配置时 aiTap 会失败，回退 playwright）
  if (PlaywrightAgent && process.env.AI_TEST_UI_USE_MIDSCENE === 'true') return 'midscene';
  if (playwright) return 'playwright';
  return 'simulated';
}

// ---------- 语义化 playwright 转换 ----------
// 把 Midscene 风格脚本（agent.aiXxx）转换为 playwright 语义操作：
//   aiInput('用户名','admin')  → getByLabel（label 关联 input）
//   aiTap('登录')              → 优先 button role，回退文本
//   aiWaitFor / aiAssert       → 文本可见等待（稳定断言）
function convertToPlaywright(code) {
  let out = code;
  out = out.replace(/await agent\.aiNavigate\(['"]([^'"]+)['"]\)/g,
    "await page.goto('$1', { waitUntil: 'domcontentloaded', timeout: 20000 });");
  out = out.replace(/await agent\.aiInput\(['"]([^'"]+)['"],\s*['"]([^'"]+)['"]\)/g,
    "await inputByLabel('$1', '$2');");
  out = out.replace(/await agent\.aiTap\(['"]([^'"]+)['"]\)/g,
    "await clickByText('$1');");
  out = out.replace(/await agent\.aiWaitFor\(['"]([^'"]+)['"]\)/g,
    "await page.getByText('$1', { exact: false }).first().waitFor({ timeout: 10000 });");
  out = out.replace(/await agent\.aiAssert\(['"]([^'"]+)['"]\)/g,
    "await assertText('$1');");
  // 清空输入框的常规操作
  out = out.replace(/await page\.getByLabel\(['"]([^'"]+)['"]\)\.clear\(\)/g,
    "await inputByLabel('$1', '');");
  return out;
}

// ---------- 真实执行（playwright 子进程 runner） ----------
// 用 node 子进程执行 runner 模板（模块级代码），避免 new Function 导致 Playwright async context 丢失。
async function runReal(testCode, baseUrl, timeoutMs) {
  const useMidscene = mode() === 'midscene' && PlaywrightAgent;
  const agentSetup = useMidscene
    ? "const { PlaywrightAgent } = require('@midscene/web'); const agent = new PlaywrightAgent(page);"
    : '';
  const converted = useMidscene ? testCode : convertToPlaywright(testCode);
  const runner = RUNNER_TEMPLATE
    .replace('/*@@AGENT_SETUP@@*/', agentSetup)
    .replace('/*@@CONVERTED_CODE@@*/', converted);
  const tmpFile = path.join(__dirname, `.ui_runner_${Date.now()}_${Math.floor(Math.random() * 9999)}.js`);
  fs.writeFileSync(tmpFile, runner, 'utf8');
  try {
    const { stdout } = await execFileP(process.execPath, [tmpFile], {
      timeout: timeoutMs || 120000,
      maxBuffer: 20 * 1024 * 1024,
    });
    const m = stdout.match(/__UI_RESULT__(\{[\s\S]*\})/);
    if (!m) {
      return { status: 'error', error: 'runner 无结果输出: ' + stdout.slice(-200), results: [] };
    }
    const r = JSON.parse(m[1]);
    return {
      status: r.status,
      error: r.error || '',
      logs: r.logs || [],
      screenshots: r.screenshots || [],
      results: [{
        name: 'ui_flow',
        status: r.status === 'success' ? 'passed' : 'failed',
        error: r.error || '',
      }],
    };
  } catch (e) {
    return {
      status: 'error',
      error: 'runner 执行异常: ' + String((e && e.message) || e).slice(0, 300),
      results: [],
    };
  } finally {
    fs.unlinkSync(tmpFile);
  }
}

const RUNNER_TEMPLATE = fs.readFileSync(path.join(__dirname, 'runner_template.js'), 'utf8');

// ---------- 模拟执行（演示模式） ----------
async function runSimulated(testCode, baseUrl) {
  const steps = (testCode.match(/await agent\.ai\w+/g) || []).length;
  const hasAssert = testCode.includes('aiAssert');
  await new Promise((r) => setTimeout(r, Math.min(800, 100 + steps * 80)));
  return {
    status: hasAssert ? 'success' : 'failed',
    simulated: true,
    results: [{
      name: 'ui_flow',
      status: hasAssert ? 'passed' : 'failed',
      error: hasAssert ? '' : '脚本缺少断言节点（aiAssert）',
    }],
    logs: [`模拟执行 ${steps} 个步骤（演示模式，未启动浏览器）`],
    screenshots: [],
  };
}

// ---------- 路由 ----------
app.post('/api/ui/run', async (req, res) => {
  const { task_id, test_code, base_url, timeout } = req.body || {};
  if (!test_code || !base_url) {
    return res.status(400).json({ status: 'error', error: '缺少 test_code 或 base_url' });
  }
  log('info', 'ui_run_start', { task_id, base_url, mode: mode() });
  const t0 = Date.now();
  let result;
  try {
    if (mode() === 'simulated') {
      result = await runSimulated(test_code, base_url);
    } else {
      result = await runReal(test_code, base_url, timeout || 60000);
    }
  } catch (e) {
    result = { status: 'error', error: String(e.message || e).slice(0, 500), results: [] };
  }
  result.duration_ms = Date.now() - t0;
  result.mode = mode();
  log('info', 'ui_run_done', { task_id, status: result.status, duration_ms: result.duration_ms, mode: result.mode });
  res.json(result);
});

app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', mode: mode() });
});

const PORT = process.env.PORT || 8399;
app.listen(PORT, () => {
  log('info', 'ui_service_started', { port: PORT, mode: mode(), has_midscene: !!PlaywrightAgent, has_playwright: !!playwright });
  if (mode() === 'simulated') {
    log('warn', '未安装 playwright/midscene，运行在模拟模式；执行 npm install --registry=https://registry.npmmirror.com 与 npx playwright install chromium 后重启可启用真实浏览器执行', {});
  }
});
