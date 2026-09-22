/**
 * 视觉核对入口：绕过 App 的登录墙，直接挂载**真实** WelcomeScreen，
 * 用真实 Tailwind 产物渲染，供 `npm run visual-check` 截图 + 量取盒模型。
 *
 *   ?trial=1 → role='test'（体验账号态：多一条体验横幅）
 *   ?admin=1 → role='admin'（tab 栏多一个 🛡️ 管理）
 *   ?tab=profile            → 自动切到「📊 我的画像」tab
 *   ?tab=profile&state=new  → 画像页 + 新用户数据（practiced 为空、32 tag 全 0）
 *   ?tab=profile&state=some → 画像页 + 已练习数据（2 个 tag 有分，其余未开始）
 *
 * 画像页需要 mock 两个接口（/auth/me/profile 与 /auth/me/profile/v2），
 * 否则 ProfileView 会显示「暂无画像数据」，新 UI 根本渲染不出来。
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
const tab = qs.get('tab');
const profileState = qs.get('state') || 'new';

const user = {
  id: 1,
  email: isTrial ? 'trial@example.com' : 'demo@example.com',
  role: isAdmin ? 'admin' : isTrial ? 'test' : 'user',
};

// 只伪造本地身份，不发真实请求（WelcomeScreen 默认 tab 不触发任何 fetch）
setAuth('visual-check-token', user);

// ── 画像页：mock 后端两个画像接口 ──
const TAG_NAMES: Record<string, string> = {
  array_basics: '数组基础', array_two_pointers: '双指针', array_sliding_window: '滑动窗口',
  array_binary_search: '二分查找', array_prefix_sum: '前缀和', array_sorting: '排序',
  linkedlist_basics: '链表基础', linkedlist_two_pointers: '链表双指针', linkedlist_cycle: '环检测',
  stack_basics: '栈基础', queue_deque: '队列/双端队列', monotonic_stack: '单调栈',
  heap_priority_queue: '堆/优先队列', tree_dfs: '树 DFS', tree_bfs: '树 BFS', tree_bst: '二叉搜索树',
  graph_dfs: '图 DFS', graph_bfs: '图 BFS', graph_topo: '拓扑排序', union_find: '并查集',
  dp_1d: '一维 DP', dp_multidim: '多维 DP', dp_interval: '区间 DP', dp_tree: '树形 DP',
  string_basics: '字符串基础', string_pattern: '字符串匹配', string_dp: '字符串 DP',
  backtrack: '回溯', greedy: '贪心', bit_manip: '位运算', math_number_theory: '数论', design: '设计',
};

function buildProfileV2() {
  const allTags = Object.keys(TAG_NAMES);
  const practiced = profileState === 'new' ? [] : ['array_basics', 'array_two_pointers'];
  const prof: Record<string, number> = {};
  for (const t of allTags) prof[t] = 0;
  if (practiced.includes('array_basics')) prof.array_basics = 0.17;
  if (practiced.includes('array_two_pointers')) prof.array_two_pointers = 0.42;
  return {
    prof,
    prof_elo_raw: {},
    stab: {},
    forget: {},
    practiced,
    tag_names: TAG_NAMES,
  };
}

function buildProfileV1() {
  const dims = ['correctness', 'datastruct', 'perf', 'algo', 'impl', 'debug'];
  return {
    attempts: profileState === 'new' ? 0 : 3,
    error_modes: Object.fromEntries(dims.map((d) => [d, {}])),
  };
}

if (tab === 'profile' && qs.get('mock') !== '0') {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.includes('/auth/me/profile/v2')) {
      return new Response(JSON.stringify(buildProfileV2()), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/auth/me/profile')) {
      return new Response(JSON.stringify(buildProfileV1()), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return realFetch(input as RequestInfo, init);
  };
}

/** React 挂载后点一下「📊 我的画像」tab（visual-check 没有交互能力，只能自点） */
function clickProfileTab(attempt = 0) {
  const btn = [...document.querySelectorAll('button')].find((b) =>
    /我的画像/.test(b.textContent || ''),
  );
  if (btn) {
    btn.click();
    return;
  }
  if (attempt < 60) setTimeout(() => clickProfileTab(attempt + 1), 25);
}

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

if (tab === 'profile') clickProfileTab();
