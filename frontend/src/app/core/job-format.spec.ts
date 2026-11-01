import { duration, eta, progressFraction } from './job-format';
import { JobRun } from './models';

const base: JobRun = {
  status: 'running', state: 'running', total: 100, done: 25, failed: 0, current: 'x',
  detail: {}, message: null, started_at: '2026-10-31T00:00:00Z', heartbeat_at: '2026-10-31T00:10:00Z', finished_at: null, stop_requested: false,
};

describe('job formatting', () => {
  it('formats durations', () => {
    expect(duration(42)).toBe('42s');
    expect(duration(125)).toBe('2m 05s');
    expect(duration(3 * 3600 + 7 * 60)).toBe('3h 07m');
  });

  it('computes progress', () => {
    expect(progressFraction(base)).toBe(0.25);
    expect(progressFraction({ ...base, total: null })).toBeNull();
  });

  it('estimates remaining time from the pace so far', () => {
    // 25 items in 10 minutes -> 75 left at the same pace = 30 minutes.
    expect(eta(base, Date.parse('2026-10-31T00:10:00Z'))).toBe('30m 00s');
    expect(eta({ ...base, done: 0 }, Date.now())).toBeNull();
    expect(eta({ ...base, done: 100 }, Date.now())).toBeNull();
  });

  it('leaves skipped items (a resume) out of the pace', () => {
    // 60 done, 50 skipped instantly: 10 fetched in 10 min -> 40 left = 40 min, not 6m 40s.
    const resumed = { ...base, done: 60, detail: { skipped: 50 } };
    expect(eta(resumed, Date.parse('2026-10-31T00:10:00Z'))).toBe('40m 00s');
    // Nothing fetched yet: no pace to go on.
    expect(eta({ ...resumed, done: 50 }, Date.now())).toBeNull();
  });
});
