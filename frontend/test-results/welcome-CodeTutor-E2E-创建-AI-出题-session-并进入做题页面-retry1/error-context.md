# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: welcome.spec.ts >> CodeTutor E2E >> 创建 AI 出题 session 并进入做题页面
- Location: tests\e2e\welcome.spec.ts:20:3

# Error details

```
Test timeout of 30000ms exceeded.
```

```
Error: expect(locator).toBeVisible() failed

Locator: locator('text=运行')
Expected: visible
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 30000ms
  - waiting for locator('text=运行')

```

```yaml
- paragraph: 出题中，请稍候...
- paragraph: 🚀 开始生成题目...
- paragraph: 正在调用大模型生成题目…
- paragraph: 第 1/2 次尝试 — 生成中…
```

# Test source

```ts
  1  | import { test, expect } from '@playwright/test';
  2  | 
  3  | test.describe('CodeTutor E2E', () => {
  4  | 
  5  |   test('欢迎页加载正常，显示标题和标签', async ({ page }) => {
  6  |     await page.goto('/');
  7  |     await expect(page.locator('text=CodeTutor Agent')).toBeVisible();
  8  |     await expect(page.locator('text=AI 出题')).toBeVisible();
  9  |     await expect(page.locator('text=从题库选')).toBeVisible();
  10 |     await expect(page.locator('text=LeetCode 链接')).toBeVisible();
  11 |   });
  12 | 
  13 |   test('切换到「我的画像」tab 显示画像信息', async ({ page }) => {
  14 |     await page.goto('/');
  15 |     await page.click('text=📊 我的画像');
  16 |     await expect(page.locator('text=熟练度')).toBeVisible();
  17 |     await expect(page.locator('text=稳定性')).toBeVisible();
  18 |   });
  19 | 
  20 |   test('创建 AI 出题 session 并进入做题页面', async ({ page }) => {
  21 |     await page.goto('/');
  22 |     // 选择知识点
  23 |     await page.click('text=数组');
  24 |     // 选择难度
  25 |     await page.click('text=Easy');
  26 |     // 点击开始练习
  27 |     await page.click('text=开始练习');
  28 |     // 等待出题完成（会进入 main 页面）
  29 |     await page.waitForURL('**/');
  30 |     // 验证页面切换到做题状态
> 31 |     await expect(page.locator('text=运行')).toBeVisible({ timeout: 30000 });
     |                                           ^ Error: expect(locator).toBeVisible() failed
  32 |   });
  33 | 
  34 |   test('管理后台可登录', async ({ page }) => {
  35 |     await page.goto('/');
  36 |     // 点击管理按钮
  37 |     await page.click('text=🛡️ 管理');
  38 |     // 输入密码
  39 |     await page.fill('input[type="password"]', '');
  40 |     await page.click('text=进入');
  41 |     // 验证进入管理页面
  42 |     await expect(page.locator('text=管理页面')).toBeVisible({ timeout: 5000 });
  43 |   });
  44 | 
  45 | });
```