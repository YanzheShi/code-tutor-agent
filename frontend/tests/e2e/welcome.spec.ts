import { test, expect } from '@playwright/test';

test.describe('CodeTutor E2E', () => {

  test('欢迎页加载正常，显示标题和标签', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('text=CodeTutor Agent')).toBeVisible();
    await expect(page.locator('text=AI 出题')).toBeVisible();
    await expect(page.locator('text=从题库选')).toBeVisible();
  });

  test('切换到「我的画像」tab 显示画像信息', async ({ page }) => {
    await page.goto('/');
    await page.click('text=📊 我的画像');
    // 使用 exact 匹配避免 strict mode 冲突
    await expect(page.getByText('熟练度', { exact: true })).toBeVisible();
    await expect(page.getByText('稳定性', { exact: true })).toBeVisible();
  });

  test('创建 AI 出题 session 并进入做题页面', async ({ page }) => {
    test.setTimeout(120000);  // 出题可能需要较长时间
    await page.goto('/');
    // 选择知识点
    await page.click('text=数组');
    // 选择难度
    await page.click('text=Easy');
    // 点击开始练习
    await page.click('text=开始练习');
    // 等待 loading 完成并进入 main 页面（出现"提交"按钮即表示出题完成）
    await expect(page.getByText('提交')).toBeVisible({ timeout: 90000 });
  });

  test('管理后台可登录', async ({ page }) => {
    await page.goto('/');
    // 点击管理按钮
    await page.click('text=🛡️ 管理');
    // 输入密码
    await page.fill('input[type="password"]', '');
    await page.click('text=进入');
    // 验证进入管理页面
    await expect(page.locator('text=管理页面')).toBeVisible({ timeout: 5000 });
  });

});