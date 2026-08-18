import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import MessageList from '../src/components/RightPanel/MessageList';

const tutorMsg = [{ role: 'tutor' as const, content: '测试消息' }];

describe('MessageList 无判题短报横幅', () => {
  it('does not show 答案不对 banner even when verdict is WA', () => {
    render(<MessageList messages={tutorMsg} verdict="WA" hintLevel={0} />);
    expect(screen.queryByText('答案不对，再检查一下逻辑')).not.toBeInTheDocument();
  });
});