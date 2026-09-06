// VITE_API_BASE='/'（docker 生产）必须归一为空串：'/' + '/session' 会拼成 '//session'，
// 浏览器按协议相对 URL 解析 → host 变成 'session' → ERR_NAME_NOT_RESOLVED。
const _rawBase = (import.meta.env.VITE_API_BASE as string) || '';
export const API_BASE = _rawBase === '/' ? '' : (_rawBase || 'http://localhost:8765');