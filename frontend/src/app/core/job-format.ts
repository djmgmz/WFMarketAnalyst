// How jobs read in the sidebar and the Overview panel. Everything here keys off a
// job's definition (kind, resumable, scoped) from the backend, never its name.

import type { IconName } from '../shared/icon.component';
import { formatNumber, timeAgo } from './format';
import { Job, JobRun, JobState, JobWithRun } from './models';

/** Status label + icon: state is never carried by color alone. */
export const STATE_LABELS: Record<JobState, { icon: IconName; label: string }> = {
  running: { icon: 'running', label: 'Running' },
  stalled: { icon: 'warning', label: 'Not responding' },
  finished: { icon: 'check', label: 'Finished' },
  stopped: { icon: 'stopped', label: 'Stopped' },
  failed: { icon: 'failed', label: 'Failed' },
};

export function seconds(fromIso: string, now: number): number {
  return Math.max(0, (now - Date.parse(fromIso)) / 1000);
}

export function duration(totalSeconds: number): string {
  const s = Math.floor(totalSeconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, '0')}m`;
}

export function progressFraction(run: JobRun): number | null {
  return run.total ? Math.min(1, run.done / run.total) : null;
}

/** Remaining time from the average pace so far; null until there is a pace. */
export function eta(run: JobRun, now: number): string | null {
  const worked = run.done - (run.detail.skipped ?? 0); // skipped items (a resume) took no time
  if (!run.total || worked <= 0 || run.done >= run.total) return null;
  return duration((seconds(run.started_at, now) / worked) * (run.total - run.done));
}

export const isActive = (job: Job): job is JobWithRun => job.run?.state === 'running' || job.run?.state === 'stalled';

/** One-line status for the sidebar. */
export function pill(job: JobWithRun): string {
  if (job.run.state === 'stalled') return `${job.title} not responding`;
  if (job.kind === 'stream') return `${job.title} · ${formatNumber(job.run.done)}`;
  const fraction = progressFraction(job.run);
  return fraction === null ? `${job.title}…` : `${job.title} ${Math.round(fraction * 100)}%`;
}

/** Main line and secondary line for a job card. */
export function describe(job: JobWithRun, now: number): { summary: string; sub: string | null } {
  const run = job.run;
  const elapsed = duration(seconds(run.started_at, now));

  if (run.state !== 'running') {
    const verb = run.state === 'stalled' ? 'Last heard from' : 'Ended';
    const result = job.kind === 'stream' ? `${formatNumber(run.done)} ${job.unit} recorded` : run.message;
    const failure = run.state === 'failed' && job.kind === 'stream' ? run.message : null;
    return {
      summary: `${verb} ${timeAgo(run.finished_at ?? run.heartbeat_at, now)}`,
      sub: [result, failure].filter(Boolean).join(' · ') || null,
    };
  }

  if (job.kind === 'stream') {
    const d = run.detail;
    const summary = [d.connection ?? 'connecting', `${formatNumber(run.done)} new ${job.unit}`, `up ${elapsed}`].join(' · ');
    const last = d.last_order_at ? `Latest ${timeAgo(d.last_order_at, now)}: ${run.current}` : 'Waiting for the first one…';
    return { summary, sub: last + (d.snapshots ? ` · ${d.snapshots} watchlist prices` : '') };
  }

  if (run.total === null) {
    return { summary: `Working on ${run.current ?? 'setup'}…`, sub: `Elapsed ${elapsed}` };
  }

  const pct = Math.round((progressFraction(run) ?? 0) * 100);
  const remaining = eta(run, now);
  const parts = [
    `${formatNumber(run.done)} / ${formatNumber(run.total)} (${pct}%)`,
    remaining ? `about ${remaining} left` : `elapsed ${elapsed}`,
    ...(run.failed ? [`${run.failed} failed`] : []),
  ];
  const context = run.detail.scope ?? run.detail.phase;
  const sub = [context, run.current ? `now: ${run.current}` : null].filter(Boolean).join(' · ') || null;
  return { summary: parts.join(' · '), sub };
}
