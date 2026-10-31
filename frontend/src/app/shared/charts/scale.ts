// Minimal scale helpers for the hand-built SVG charts.

export interface Point {
  t: number; // epoch ms
  v: number | null;
}

export interface RangePoint {
  t: number;
  low: number;
  high: number;
}

export interface Series {
  key: string;
  label: string;
  color: string; // a CSS custom property reference, e.g. 'var(--series-1)'
  points: Point[];
  /** Drawn dashed: a projection, not recorded data. */
  dashed?: boolean;
  /** Optional likely range around the series, drawn as a light band. */
  range?: RangePoint[];
}

export type Scale = (value: number) => number;

export function linear(domain: [number, number], range: [number, number]): Scale {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (value) => r0 + ((value - d0) / span) * (r1 - r0);
}

/** Round tick values ("nice" numbers) covering [min, max]. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (min === max) {
    const pad = Math.abs(min) * 0.1 || 1;
    min -= pad;
    max += pad;
  }
  const raw = (max - min) / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const residual = raw / magnitude;
  const step = (residual >= 5 ? 10 : residual >= 2 ? 5 : residual >= 1 ? 2 : 1) * magnitude;
  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= end + step / 2; v += step) {
    ticks.push(Math.round(v * 1e6) / 1e6);
  }
  return ticks;
}

/** Evenly spaced indexes for x-axis labels so they never crowd. */
export function spacedIndexes(length: number, maxLabels: number): number[] {
  if (length <= maxLabels) return Array.from({ length }, (_, i) => i);
  const step = Math.ceil(length / maxLabels);
  const out: number[] = [];
  for (let i = 0; i < length; i += step) out.push(i);
  return out;
}

/** SVG path for a line, breaking at null values. */
export function linePath(points: Point[], x: Scale, y: Scale): string {
  let d = '';
  let pen = false;
  for (const p of points) {
    if (p.v === null) {
      pen = false;
      continue;
    }
    d += `${pen ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

/** Column with 4px rounded data-end and a square baseline. */
export function columnPath(x: number, y: number, width: number, baseline: number, radius = 4): string {
  const h = baseline - y;
  if (h <= 0) return '';
  const r = Math.min(radius, width / 2, h);
  return (
    `M${x},${baseline}V${y + r}Q${x},${y} ${x + r},${y}` +
    `H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${baseline}Z`
  );
}

/** Index of the timestamp closest to t (timestamps sorted ascending). */
export function nearestIndex(timestamps: number[], t: number): number {
  let lo = 0;
  let hi = timestamps.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (timestamps[mid] < t) lo = mid;
    else hi = mid;
  }
  return Math.abs(timestamps[lo] - t) <= Math.abs(timestamps[hi] - t) ? lo : hi;
}

/** Plot margins shared by every chart; the right margin leaves room for end labels. */
export const CHART_MARGIN = { top: 12, right: 64, bottom: 28, left: 52 } as const;

export interface YTick {
  v: number;
  y: number;
  label: string;
}

export interface XTick {
  t: number;
  x: number;
  label: string;
}

/** Left edge for a tooltip anchored at x: to the right of the pointer, flipped near the edge. */
export function tooltipLeft(x: number, width: number, tooltipWidth = 160): number {
  return x + tooltipWidth > width ? x - tooltipWidth : x + 12;
}

/** Closed SVG path for a band between low and high (points sorted by t). */
export function bandPath(range: RangePoint[], x: Scale, y: Scale): string {
  if (range.length < 2) return '';
  const top = range.map((p) => `${x(p.t).toFixed(1)},${y(p.high).toFixed(1)}`);
  const bottom = [...range].reverse().map((p) => `${x(p.t).toFixed(1)},${y(p.low).toFixed(1)}`);
  return `M${top.join('L')}L${bottom.join('L')}Z`;
}
