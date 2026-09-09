import { test, expect, type Page } from '@playwright/test';

/**
 * CodeTutor E2E（2026-09-09 更新到多用户版 UI 契约）：
 *
 * - 访客落地页（GuestWelcome）：「你的 AI 编程私教」+ 开始使用 → AuthModal
 * - 登录后 WelcomeScreen：tabs = Agent 导师 / 从题库选 / 📊 我的画像 / 📋 我的提交 /（admin）🛡️ 管理
 * - 旧版「选数组/Easy → 开始练习」与「管理密码框」流程已随多用户改造下线
 *
 * 凭据从环境变量注入（绝不硬编码）：E2E_EMAIL / E2E_PASSWORD（需 admin 角色）。
 * 未设置时登录类用例自动 skip，访客用例始终可跑。
 */
const E2E_EMAIL = process.env.E2E_EMAIL || '';
const E2E_PASSWORD = process.env.E2E_PASSWORD || '';
const HAS_AUTH = Boolean(E2E_EMAIL && E2E_PASSWORD);

/** 访客主页 → AuthModal → 登录 → 整页刷新进 WelcomeScreen。 */
async function login(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: '开始使用' }).click();
  await page.fill('input[type="email"]', E2E_EMAIL);
  await page.fill('input[type="password"]', E2E_PASSWORD);
  await page.locator('form button[type="submit"]').click();
  // 登录成功 → onLoggedIn 整页刷新 → WelcomeScreen（tab 栏含「从题库选」）
  await expect(page.getByText('从题库选')).toBeVisible({ timeout: 20000 });
}

test.describe('CodeTutor E2E — 访客落地页', () => {
  test('欢迎页加载正常，展示产品价值与登录入口', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByText('你的 AI 编程私教')).toBeVisible();
    await expect(page.getByRole('button', { name: '开始使用' })).toBeVisible();
    await expect(page.getByText('题库覆盖这些方向')).toBeVisible();
    // 登录墙前先见特性卡（AI 对话式出题）
    await expect(page.getByText('AI 对话式出题')).toBeVisible();
  });
});

test.describe('CodeTutor E2E — 登录后', () => {
  test.skip(!HAS_AUTH, '需要 E2E_EMAIL / E2E_PASSWORD 环境变量（admin 账号）');

  test('切换到「我的画像」tab 显示能力画像', async ({ page }) => {
    await login(page);
    await page.getByRole('button', { name: /我的画像/ }).click();
    // v2 画像页：能力画像雷达图（六维 + 均分），旧版「熟练度/稳定性」术语已下线
    await expect(page.getByText('能力画像')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('均分')).toBeVisible();
  });

  test('从题库选进入做题页面', async ({ page }) => {
    test.setTimeout(60000);
    await login(page);
    await page.click('text=从题库选');
    // 题库为空属环境问题（dev 库无题），跳过而非误报
    const emptyPool = page.getByText('题库为空');
    if (await emptyPool.isVisible({ timeout: 5000 }).catch(() => false)) {
      test.skip(true, '题库为空，跳过做题页 e2e');
    }
    // 选第一道题 → 开始练习 → 进入做题页（「提交」按钮；exact 避免撞「提交记录」tab）
    await page.locator('button', { hasText: /\d+\. / }).first().click();
    await page.getByRole('button', { name: '开始练习' }).click();
    await expect(page.getByRole('button', { name: '提交', exact: true })).toBeVisible({ timeout: 30000 });
  });

  test('管理后台可进入（admin 角色）', async ({ page }) => {
    await login(page);
    // 管理入口仅 admin 角色可见
    await expect(page.getByText('🛡️ 管理')).toBeVisible({ timeout: 10000 });
    await page.click('text=🛡️ 管理');
    await expect(page.getByText('🛡️ 管理页面')).toBeVisible({ timeout: 10000 });
  });
});
