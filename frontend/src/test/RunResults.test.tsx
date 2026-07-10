import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import RunResults from '../components/RunResults';

describe('RunResults', () => {
  it('shows placeholder when no results and not running', () => {
    render(<RunResults results={null} running={false} />);
    expect(screen.getByText('点击「运行」查看结果')).toBeInTheDocument();
  });

  it('shows running indicator', () => {
    render(<RunResults results={null} running={true} />);
    expect(screen.getByText('运行中...')).toBeInTheDocument();
  });

  it('displays pass/fail count', () => {
    const results = [
      { test_case_id: 1, passed: true, status: 'Passed', detail: 'OK', expected: '1', runtime_ms: 10, memory_kb: 100, input_args: [] },
      { test_case_id: 2, passed: false, status: 'Wrong Answer', detail: 'got 2', expected: '1', runtime_ms: 10, memory_kb: 100, input_args: [] },
    ];
    render(<RunResults results={results} running={false} />);
    expect(screen.getByText('运行结果: 1/2 通过')).toBeInTheDocument();
  });

  it('shows all pass in green', () => {
    const results = [{ test_case_id: 1, passed: true, status: 'Passed', detail: 'OK', expected: '1', runtime_ms: 10, memory_kb: 100, input_args: [] }];
    render(<RunResults results={results} running={false} />);
    expect(screen.getByText('✓')).toBeInTheDocument();
    expect(screen.getByText(/Judge0/)).toBeInTheDocument();
  });
});