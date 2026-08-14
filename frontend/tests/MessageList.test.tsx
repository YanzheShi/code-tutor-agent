import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import MessageList from '../src/components/RightPanel/MessageList';

const tutorMsg = [{ role: 'tutor' as const, content: '测试消息' }];

describe('MessageList 判题短报', () => {
  it('shows 答案不对 banner only when verdict is WA', () => {
    render(<MessageList messages={tutorMsg} verdict="WA" hintLevel={0} />);
    expect(screen.getByText('答案不对，再检查一下逻辑')).toBeInTheDocument();
  });

  it('hides the banner when verdict is null (e.g. after a fully-passing run)', () => {
    render(<MessageList messages={tutorMsg} verdict={null} hintLevel={0} />);
    expect(screen.queryByText('答案不对，再检查一下逻辑')).not.toBeInTheDocument();
  });

  it('does not show the banner on AC or other verdicts', () => {
    render(<MessageList messages={tutorMsg} verdict="AC" hintLevel={0} />);
    expect(screen.queryByText('答案不对，再检查一下逻辑')).not.toBeInTheDocument();
  });
});