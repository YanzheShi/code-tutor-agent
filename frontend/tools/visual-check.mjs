/**
 * 前端视觉核对（visual smoke check）—— `npm run visual-check`
 *
 * 为什么需要它：
 *   vitest 跑在 jsdom，**没有布局引擎**。元素重叠、溢出、被裁切这类视觉 bug
 *   它一律看不见 —— 单测全绿也保不住页面长什么样。2026-09-20 主页顶栏
 *   `💬 反馈` 压住 `🤖 CodeTutor Agent` 就是这么漏过去的（单测全过，页面是坏的）。
 *   同时沙箱里 `vite dev` 起不来（safe-delete 拦截），前端没有可视化手段。
 *
 * 做法（全部绕开 vite dev）：
 *   1. vite build 单入口 → 系统 Temp 的全新目录（避开沙箱 emptyDir 的 safe-delete 拦截）
 *   2. node:http 起纯静态服务
 *   3. Playwright(chromium) 打开真实组件，截图 + `getBoundingClientRect()` 量盒模型
 *   4. 断言「顶栏工具行」与「标题块」互不重叠、且标题块未溢出卡片
 *
 * 产物：`frontend/.visual-check/<state>.png`（已 gitignore）
 * 退出码：任一态断言失败 → 1
 */
import { build } from 'vite';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, mkdir, rm } from 'node:fs/promises';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..'); // frontend/
const shotsDir = path.join(root, '.visual-check');

/** 被测状态：覆盖「按钮最多」与「最窄」两个边界 */
const STATES = [
  { name: 'user-1280', query: '', w: 1280, h: 900 },
  { name: 'trial-1280', query: '?trial=1', w: 1280, h: 900 },
  { name: 'admin-1280', query: '?admin=1', w: 1280, h: 900 },
  { name: 'trial-560', query: '?trial=1', w: 560, h: 900 },
];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
};

/** 在页面里量盒模型，并给出三条断言所需的事实（纯数据，判定留在 node 侧） */
function measureInPage() {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {
      l: Math.round(r.left),
      t: Math.round(r.top),
      r: Math.round(r.right),
      b: Math.round(r.bottom),
      w: Math.round(r.width),
      h: Math.round(r.height),
    };
  };
  const h1 = document.querySelector('h1');
  if (!h1) return { error: 'h1 not found' };
  const feedbackBtn = [...document.querySelectorAll('button')].find((b) => /反馈/.test(b.textContent || ''));
  if (!feedbackBtn) return { error: '反馈 button not found' };

  const titleBlock = h1.closest('.text-center');
  const toolRow = feedbackBtn.parentElement;
  // 打个锚点，供 node 侧裁一张工具行放大图（肉眼核对按钮样式是否一致）
  toolRow.setAttribute('data-visual-tool-row', '');
  const card = document.querySelector('.max-w-2xl');
  const cs = card ? getComputedStyle(card) : null;
  const cardPadBox = card
    ? {
        l: Math.round(card.getBoundingClientRect().left + parseFloat(cs.paddingLeft)),
        r: Math.round(card.getBoundingClientRect().right - parseFloat(cs.paddingRight)),
      }
    : null;

  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    titleBlock: rect(titleBlock),
    toolRow: rect(toolRow),
    h1: rect(h1),
    cardPadBox,
    buttons: [...toolRow.querySelectorAll('button')].map((b) => ({
      label: (b.textContent || '').trim(),
      ...rect(b),
    })),
  };
}

function overlap(a, b) {
  const x = Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l));
  const y = Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t));
  return { x, y, area: x * y };
}

async function assertStates() {
  // 1) 单体量：outDir 走环境变量，落到系统 Temp 的全新目录
  const outDir = await mkdtemp(path.join(tmpdir(), 'cta-visual-check-'));
  process.env.VISUAL_CHECK_OUT = outDir;
  await build({
    configFile: path.join(root, 'vite.visual.config.ts'),
    logLevel: 'warn',
  });

  // 2) 纯静态服务（不用 vite dev / preview）
  const server = createServer(async (req, res) => {
    const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
    const rel = urlPath === '/' ? '/visual-check.html' : urlPath;
    const file = path.join(outDir, rel);
    if (!file.startsWith(outDir)) {
      res.writeHead(403).end('forbidden');
      return;
    }
    try {
      const body = await readFile(file);
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
      res.end(body);
    } catch {
      res.writeHead(404).end('not found');
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;

  // 3) 截图 + 量取
  await mkdir(shotsDir, { recursive: true });
  const browser = await chromium.launch();
  const results = [];
  for (const st of STATES) {
    const page = await browser.newPage({
      viewport: { width: st.w, height: st.h },
      deviceScaleFactor: 2,
    });
    await page.goto(`http://127.0.0.1:${port}/visual-check.html${st.query}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(300);
    const m = await page.evaluate(measureInPage);
    await page.screenshot({ path: path.join(shotsDir, `${st.name}.png`) });
    // 工具行局部放大图（deviceScaleFactor=2 → 2x 分辨率），比整页缩略图更容易看出按钮样式/边框差异
    const toolRowLoc = page.locator('[data-visual-tool-row]');
    if (await toolRowLoc.count()) {
      await toolRowLoc.screenshot({ path: path.join(shotsDir, `${st.name}-toolbar.png`), scale: 'device' });
    }
    await page.close();

    if (m.error) {
      results.push({ state: st.name, pass: false, reason: m.error });
      continue;
    }
    const ov = overlap(m.titleBlock, m.toolRow);
    const stacked = m.toolRow.b <= m.titleBlock.t;
    const inside =
      !m.cardPadBox ||
      (m.titleBlock.l >= m.cardPadBox.l - 1 && m.titleBlock.r <= m.cardPadBox.r + 1);
    const pass = ov.area === 0 && stacked && inside;
    results.push({
      state: st.name,
      pass,
      reason: pass
        ? 'ok'
        : [
            ov.area > 0 ? `标题块与工具行重叠 ${ov.x}×${ov.y}px` : null,
            !stacked ? `未分层（工具行 b=${m.toolRow.b} > 标题块 t=${m.titleBlock.t}）` : null,
            !inside ? '标题块溢出卡片内边距' : null,
          ]
            .filter(Boolean)
            .join('; '),
      titleBlock: m.titleBlock,
      toolRow: m.toolRow,
      buttons: m.buttons.map((b) => `${b.label}(${b.w})`).join(' '),
    });
  }
  await browser.close();
  server.close();
  await rm(outDir, { recursive: true, force: true }).catch(() => {});

  // 4) 汇报
  console.log('\n视觉核对结果（.visual-check/*.png）\n');
  for (const r of results) {
    const flag = r.pass ? 'PASS' : 'FAIL';
    console.log(`  [${flag}] ${r.state.padEnd(12)} ${r.reason}`);
    if (r.titleBlock) {
      console.log(
        `         工具行 y=${r.toolRow.t}-${r.toolRow.b}｜标题块 y=${r.titleBlock.t}-${r.titleBlock.b}` +
          ` x=${r.titleBlock.l}-${r.titleBlock.r}｜按钮宽度 ${r.buttons}`,
      );
    }
  }
  const failed = results.filter((r) => !r.pass);
  console.log(`\n  ${results.length - failed.length}/${results.length} 通过\n`);
  if (failed.length) process.exitCode = 1;
}

await assertStates();
