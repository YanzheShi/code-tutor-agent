# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: welcome.spec.ts >> CodeTutor E2E >> 切换到「我的画像」tab 显示画像信息
- Location: tests\e2e\welcome.spec.ts:13:3

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('text=熟练度')
Expected: visible
Error: strict mode violation: locator('text=熟练度') resolved to 2 elements:
    1) <span class="text-ct-muted">熟练度</span> aka getByText('熟练度', { exact: true })
    2) <p class="text-xs text-ct-muted text-center">画像在每次判题后自动更新，根据熟练度规划下一题难度</p> aka getByText('画像在每次判题后自动更新，根据熟练度规划下一题难度')

Call log:
  - Expect "toBeVisible" with timeout 5000ms
  - waiting for locator('text=熟练度')

```

# Page snapshot

```yaml
- generic [ref=e4]:
  - generic [ref=e5]:
    - heading "🤖 CodeTutor Agent" [level=1] [ref=e6]
    - paragraph [ref=e7]: AI 编程私教 · 自主出题 · 对抗判题 · 渐进辅导
  - generic [ref=e8]:
    - button "AI 出题" [ref=e9] [cursor=pointer]
    - button "🤖 Agent 导师" [ref=e10] [cursor=pointer]
    - button "从题库选" [ref=e11] [cursor=pointer]
    - button "📊 我的画像" [active] [ref=e12] [cursor=pointer]
    - button "LeetCode 链接" [ref=e13] [cursor=pointer]
    - button "🛡️ 管理" [ref=e14] [cursor=pointer]
  - generic [ref=e15]:
    - heading "📊 我的画像" [level=2] [ref=e16]
    - generic [ref=e17]:
      - generic [ref=e19]:
        - generic [ref=e20]: 熟练度
        - generic [ref=e21]: 65%
      - generic [ref=e25]:
        - generic [ref=e26]: 稳定性
        - generic [ref=e27]: 55%
      - generic [ref=e30]:
        - generic [ref=e31]:
          - text: 做题数
          - paragraph [ref=e32]: "1"
        - generic [ref=e33]:
          - text: 距离上次
          - paragraph [ref=e34]: 0 天
    - paragraph [ref=e35]: 画像在每次判题后自动更新，根据熟练度规划下一题难度
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
> 16 |     await expect(page.locator('text=熟练度')).toBeVisible();
     |                                            ^ Error: expect(locator).toBeVisible() failed
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
  31 |     await expect(page.locator('text=运行')).toBeVisible({ timeout: 30000 });
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