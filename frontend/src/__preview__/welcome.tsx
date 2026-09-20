/**
 * 视觉核对入口：绕过 App 的登录墙，直接挂载**真实** WelcomeScreen，
 * 用真实 Tailwind 产物渲染，供 `npm run visual-check` 截图 + 量取盒模型。
 *
 *   ?trial=1 → role='test'（体验账号态：多一条体验横幅）
 *   ?admin=1 → role='admin'（tab 栏多一个 🛡️ 管理）
 *
 * 放在 `src/` 下是必须的：tailwind.config.ts 的 content 只扫 `./index.html`
 * 与 `./src/**`，放外面 Tailwind 不会为它生成类，页面会没样式。
 */
import { createRoot } from 'react-dom/client';
import '../index.css';
import WelcomeScreen from '../components/WelcomeScreen';
import { ThemeProvider } from '../hooks/useTheme';
import { setAuth } from '../api/auth';

const qs = new URLSearchParams(window.location.search);
const isTrial = qs.get('trial') === '1';
const isAdmin = qs.get('admin') === '1';

const user = {
  id: 1,
  email: isTrial ? 'trial@example.com' : 'demo@example.com',
  role: isAdmin ? 'admin' : isTrial ? 'test' : 'user',
};

// 只伪造本地身份，不发真实请求（WelcomeScreen 默认 tab 不触发任何 fetch）
setAuth('visual-check-token', user);

createRoot(document.getElementById('root')!).render(
  <ThemeProvider>
    <WelcomeScreen
      user={user}
      onStart={() => {}}
      onStartExisting={() => {}}
      onOpenAdmin={() => {}}
      onOpenSettings={() => {}}
      onLogout={() => {}}
    />
  </ThemeProvider>,
);
