// AI 测试智能体 — UI 执行 runner（子进程模板）
// server.js 读取本文件，将执行代码注入到下方 try 块内的标记处后写入临时文件，
// 用 node 子进程执行。模块级代码保证 Playwright async context 正常（避免 new Function 的 context 丢失）。
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const logs = [];
  const screenshots = [];
  const shot = async () => {
    try { screenshots.push((await page.screenshot({ fullPage: true })).toString('base64')); } catch (e) {}
  };

  // 语义化定位辅助（aiInput/aiTap/aiAssert 转换后的目标）
  function esc(s) { return s.replace(/["'\\]/g, (c) => '\\' + c); }
  async function inputByLabel(label, value) {
    const labeled = page.getByLabel(label, { exact: false });
    if (await labeled.count()) { await labeled.first().fill(value); return; }
    const row = page.locator('div.form-row:has(label:text("' + esc(label) + '"))').first();
    await row.locator('input, textarea').first().fill(value);
  }
  async function clickByText(text) {
    const btn = page.getByRole('button', { name: text, exact: false });
    if (await btn.count()) { await btn.first().click(); return; }
    await page.getByText(text, { exact: false }).first().click();
  }
  async function assertText(text) {
    await page.getByText(text, { exact: false }).first().waitFor({ timeout: 10000 });
    logs.push('[assert] 文本可见: ' + text);
  }

  try {
    /*@@AGENT_SETUP@@*/
    /*@@CONVERTED_CODE@@*/
    await shot();
    console.log('__UI_RESULT__' + JSON.stringify({ status: 'success', logs, screenshots }));
  } catch (e) {
    await shot().catch(() => {});
    console.log('__UI_RESULT__' + JSON.stringify({
      status: 'failed',
      error: String((e && e.message) || e).slice(0, 800),
      logs, screenshots,
    }));
  } finally {
    await browser.close().catch(() => {});
  }
})();
