import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import SubmissionHistory from '../src/components/SubmissionHistory';

describe('SubmissionHistory', () => {
  it('shows empty message when no submissions', () => {
    render(<SubmissionHistory submissions={[]} />);
    expect(screen.getByText('暂无提交记录')).toBeInTheDocument();
  });

  it('renders submission verdicts', () => {
    const submissions = [{
      code: 'print(1)', verdict: 'AC', timestamp: '12:00',
      judge_results: [{ phase: 'base', status: 'AC', detail: '通过', runtime_ms: 10.5 }],
      index: 1, language: 'python', hint_level_given: 0,
    }];
    render(<SubmissionHistory submissions={submissions} />);
    expect(screen.getByText('AC')).toBeInTheDocument();
    expect(screen.getByText(/base: AC/)).toBeInTheDocument();
  });

  it('renders multiple submissions in reverse order', () => {
    const submissions = [
      { code: 'v1', verdict: 'WA', timestamp: '12:01', judge_results: [], index: 1, language: 'python', hint_level_given: 0 },
      { code: 'v2', verdict: 'AC', timestamp: '12:02', judge_results: [], index: 2, language: 'python', hint_level_given: 0 },
    ];
    render(<SubmissionHistory submissions={submissions} />);
    const texts = screen.getAllByText(/AC|WA/);
    expect(texts[0]).toHaveTextContent('AC');  // reverse: AC first
    expect(texts[1]).toHaveTextContent('WA');  // then WA
  });

  it('shows RE fallback when verdict missing', () => {
    const submissions = [{
      code: '', verdict: '', timestamp: '',
      judge_results: [{ phase: 'base', status: 'RE', detail: '', runtime_ms: 0 }],
      index: 1, language: 'python', hint_level_given: 0,
    }];
    render(<SubmissionHistory submissions={submissions} />);
    expect(screen.getByText('RE')).toBeInTheDocument();
  });

  it('truncates long code', () => {
    const submissions = [{
      code: 'a'.repeat(500), verdict: 'AC', timestamp: '',
      judge_results: [], index: 1, language: 'python', hint_level_given: 0,
    }];
    render(<SubmissionHistory submissions={submissions} />);
    expect(screen.getByText(/\.\.\.$/)).toBeInTheDocument();
  });
});