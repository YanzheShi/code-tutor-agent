import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import DifficultyTopicSelector from '../src/components/LeftPanel/DifficultyTopicSelector';

function mockFetchTopics(topics: { value: string; label: string }[]) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url).endsWith('/topics')) {
      return { ok: true, json: async () => ({ topics }) } as Response;
    }
    throw new Error('unexpected fetch: ' + url);
  }));
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe('DifficultyTopicSelector topics from API', () => {
  it('renders topics from backend API (including new topics)', async () => {
    mockFetchTopics([
      { value: '数组', label: '数组' },
      { value: '数论', label: '数论' },
      { value: '图', label: '图' },
    ]);
    const onPick = vi.fn();
    render(
      <DifficultyTopicSelector
        difficulty={null} topic={null}
        onPickDifficulty={() => {}} onPickTopic={onPick} onStart={() => {}}
      />,
    );

    // 接口返回前先出兜底静态列表，返回后替换
    await waitFor(() => expect(screen.getByText('数论')).toBeInTheDocument());
    expect(screen.getByText('图')).toBeInTheDocument();
    expect(screen.queryByText('动态规划')).not.toBeInTheDocument();

    // 随机主题由前端追加在末尾
    expect(screen.getByText('随机主题')).toBeInTheDocument();

    // 点击新主题可选中
    fireEvent.click(screen.getByText('数论'));
    expect(onPick).toHaveBeenCalledWith('数论');
  });

  it('falls back to static list when the API fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));
    render(
      <DifficultyTopicSelector
        difficulty={null} topic={null}
        onPickDifficulty={() => {}} onPickTopic={() => {}} onStart={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText('双指针')).toBeInTheDocument());
    expect(screen.getByText('动态规划')).toBeInTheDocument();
    expect(screen.getByText('随机主题')).toBeInTheDocument();
  });

  it('keeps static fallback visible while API is pending', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {}))); // 永不返回
    render(
      <DifficultyTopicSelector
        difficulty={null} topic={null}
        onPickDifficulty={() => {}} onPickTopic={() => {}} onStart={() => {}}
      />,
    );
    expect(screen.getByText('数组')).toBeInTheDocument();
  });
});