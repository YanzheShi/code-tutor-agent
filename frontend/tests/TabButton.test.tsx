import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TabButton, VerdictBadge } from '../src/components/TabButton';

describe('TabButton', () => {
  it('renders label text', () => {
    render(<TabButton label="题目描述" active={false} onClick={() => {}} />);
    expect(screen.getByText('题目描述')).toBeInTheDocument();
  });

  it('applies active style when active', () => {
    render(<TabButton label="代码" active={true} onClick={() => {}} />);
    const btn = screen.getByText('代码');
    expect(btn.className).toContain('border-b-2');
    expect(btn.className).toContain('text-ct-text');
  });

  it('applies inactive style when not active', () => {
    render(<TabButton label="运行" active={false} onClick={() => {}} />);
    const btn = screen.getByText('运行');
    expect(btn.className).toContain('text-ct-muted');
  });
});

describe('VerdictBadge', () => {
  it('renders AC in green', () => {
    render(<VerdictBadge verdict="AC" />);
    const el = screen.getByText('AC');
    expect(el.className).toContain('text-ct-success');
  });

  it('renders WA in warn color', () => {
    render(<VerdictBadge verdict="WA" />);
    const el = screen.getByText('WA');
    expect(el.className).toContain('text-ct-warn');
  });

  it('renders TLE in error color', () => {
    render(<VerdictBadge verdict="TLE" />);
    const el = screen.getByText('TLE');
    expect(el.className).toContain('text-ct-error');
  });

  it('falls back to muted for unknown verdict', () => {
    render(<VerdictBadge verdict="CE" />);
    const el = screen.getByText('CE');
    expect(el.className).toContain('text-ct-muted');
  });
});