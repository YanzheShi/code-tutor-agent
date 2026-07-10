import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import SubmissionHistory from '../src/components/SubmissionHistory';

const MOCK_SUBMISSIONS = [
  { code: 'print(1)', verdict: 'AC', timestamp: '12:00', judge_results: [{ phase: 'base', status: 'AC', detail: '通过', runtime_ms: 10.5 }], index: 1, language: 'python', hint_level_given: 0 },
  { code: 'v1', verdict: 'WA', timestamp: '12:01', judge_results: [], index: 2, language: 'python', hint_level_given: 0 },
  { code: 'v2', verdict: 'AC', timestamp: '12:02', judge_results: [], index: 3, language: 'python', hint_level_given: 0 },
];

function mockFetchOk(data: unknown) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true,
    json: async () => ({ submissions: data }),
  } as Response);
}

describe('SubmissionHistory', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows loading initially', () => {
    mockFetchOk([]);
    render(<SubmissionHistory problemId={1} />);
    expect(screen.getByText('加载中...')).toBeInTheDocument();
  });

  it('shows empty message when API returns empty', async () => {
    mockFetchOk([]);
    render(<SubmissionHistory problemId={1} />);
    await waitFor(() => expect(screen.getByText('暂无提交记录')).toBeInTheDocument());
  });

  it('renders submission verdicts from API', async () => {
    mockFetchOk([MOCK_SUBMISSIONS[0]]);
    render(<SubmissionHistory problemId={1} />);
    await waitFor(() => {
      expect(screen.getByText('AC')).toBeInTheDocument();
      expect(screen.getByText(/base: AC/)).toBeInTheDocument();
    });
  });

  it('renders multiple submissions in reverse order', async () => {
    mockFetchOk(MOCK_SUBMISSIONS.slice(1));
    render(<SubmissionHistory problemId={1} />);
    await waitFor(() => {
      const texts = screen.getAllByText(/AC|WA/);
      expect(texts[0]).toHaveTextContent('AC');
      expect(texts[1]).toHaveTextContent('WA');
    });
  });

  it('shows RE fallback when verdict missing', async () => {
    const sub = [{ code: '', verdict: '', timestamp: '', judge_results: [{ phase: 'base', status: 'RE', detail: '', runtime_ms: 0 }], index: 1, language: 'python', hint_level_given: 0 }];
    mockFetchOk(sub);
    render(<SubmissionHistory problemId={1} />);
    await waitFor(() => expect(screen.getByText('RE')).toBeInTheDocument());
  });

  it('truncates long code', async () => {
    const sub = [{ code: 'a'.repeat(500), verdict: 'AC', timestamp: '', judge_results: [], index: 1, language: 'python', hint_level_given: 0 }];
    mockFetchOk(sub);
    render(<SubmissionHistory problemId={1} />);
    await waitFor(() => expect(screen.getByText(/\.\.\.$/)).toBeInTheDocument());
  });
});