import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import LoadingScreen from '../src/components/LoadingScreen';

describe('LoadingScreen', () => {
  it('shows loading spinner with progress messages', () => {
    render(<LoadingScreen progressMsgs={['正在生成题目...', '✅ 已完成']} onRetry={() => {}} />);
    expect(screen.getByText('正在为你出题，请稍候...')).toBeInTheDocument();
    expect(screen.getByText('正在生成题目...')).toBeInTheDocument();
    expect(screen.getByText('✅ 已完成')).toBeInTheDocument();
  });

  it('shows friendly error card when errorMsg provided', () => {
    render(<LoadingScreen progressMsgs={[]} errorMsg="出题比预期慢了一些" onRetry={() => {}} />);
    expect(screen.getByText('出题遇到了点小状况')).toBeInTheDocument();
    expect(screen.getByText('出题比预期慢了一些')).toBeInTheDocument();
  });

  it('shows retry button on error', () => {
    render(<LoadingScreen progressMsgs={[]} errorMsg="失败" onRetry={() => {}} />);
    expect(screen.getByText('重新出题')).toBeInTheDocument();
  });

  it('does not show error section when no errorMsg', () => {
    render(<LoadingScreen progressMsgs={['加载中']} onRetry={() => {}} />);
    expect(screen.queryByText('出题遇到了点小状况')).not.toBeInTheDocument();
  });
});
