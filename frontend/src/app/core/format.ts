// Display formatting shared by pages and charts.

export const ASSETS_BASE = 'https://warframe.market/static/assets/';

export function assetUrl(path: string | null | undefined): string | null {
  return path ? ASSETS_BASE + path : null;
}

const full = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

/** 1,284 below 10,000; 12.9K above. */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–';
  return Math.abs(value) < 10_000 ? full.format(value) : compact.format(value);
}

export function formatPlat(value: number | null | undefined): string {
  return value === null || value === undefined ? '–' : `${formatNumber(value)}p`;
}

export function formatPercent(value: number, signed = true): string {
  const pct = (value * 100).toFixed(Math.abs(value) < 0.1 ? 1 : 0);
  return signed && value > 0 ? `+${pct}%` : `${pct}%`;
}

/** What a forecast horizon predicts: tomorrow's price, or the average price over the next h days. */
export function horizonLabel(horizon: number | string): string {
  return Number(horizon) === 1 ? 'Tomorrow' : `Next ${horizon} days (avg)`;
}

export function formatCredits(value: number | null | undefined): string {
  return value === null || value === undefined ? '–' : `${compact.format(value)} cr`;
}

const dayFmt = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
const hourFmt = new Intl.DateTimeFormat('en-US', { hour: 'numeric' });
const dateTimeFmt = new Intl.DateTimeFormat('en-US', {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
});

export function formatDay(t: number): string {
  return dayFmt.format(t);
}

export function formatHour(t: number): string {
  return hourFmt.format(t);
}

export function formatDateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '–';
  return dateTimeFmt.format(typeof value === 'string' ? Date.parse(value) : value);
}

export function timeAgo(value: string | null | undefined, now = Date.now()): string {
  if (!value) return 'never';
  const seconds = Math.max(0, Math.round((now - Date.parse(value)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function titleCase(tag: string): string {
  return tag.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}
