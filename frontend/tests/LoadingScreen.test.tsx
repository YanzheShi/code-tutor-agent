import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import LoadingScreen from '../src/components/LoadingScreen';

describe('LoadingScreen', () => {
  it('shows loading spinner with progress messages', () => {
    render(<LoadingScreen progressMsgs={['正在生成题目...', '✅ 已完成']} onRetry={() => {}} />);
    expect(screen.getByText('出题中，请稍候...')).toBeInTheDocument();
    expect(screen.getByText('正在生成题目...')).toBeInTheDocument();
    expect(screen.getByText('✅ 已完成')).toBeInTheDocument();
  });

  it('shows error message when errorMsg provided', () => {
    render(<LoadingScreen progressMsgs={[]} errorMsg="网络连接失败" onRetry={() => {}} />);
    expect(screen.getByText('出错了')).toBeInTheDocument();
    expect(screen.getByText('网络连接失败')).toBeInTheDocument();
  });

  it('shows retry button on error', () => {
    render(<LoadingScreen progressMsgs={[]} errorMsg="失败" onRetry={() => {}} />);
    expect(screen.getByText('重试')).toBeInTheDocument();
  });

  it('does not show error section when no errorMsg', () => {
    render(<LoadingScreen progressMsgs={['加载中']} onRetry={() => {}} />);
    expect(screen.queryByText('出错了')).not.toBeInTheDocument();
  });
});