import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { formatDay, formatHour, formatPlat } from '../../core/format';
import { ChartFrameComponent } from './chart-frame.component';
import { injectElementWidth } from './element-width';
import {
  CHART_MARGIN as M, RangePoint, Series, bandPath, linePath, linear, nearestIndex, niceTicks, spacedIndexes, tooltipLeft,
} from './scale';

/**
 * Multi-series line chart (max ~3 series): 2px lines, direct end labels, a
 * crosshair tooltip listing every series, keyboard stepping, and the frame's
 * legend / table view. A series can be dashed (a projection) and carry a range,
 * drawn as a ~10% band in its own color and shown in the tooltip and table.
 */
@Component({
  selector: 'app-line-chart',
  imports: [ChartFrameComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-chart-frame [width]="width()" [height]="height()" [yTicks]="geo().yTicks" [xTicks]="geo().xTicks"
                     [ariaLabel]="ariaLabel()">
      @if (series().length > 1) {
        <div legend class="legend">
          @for (s of series(); track s.key) {
            <span class="legend-item">
              <span class="key-line" [class.key-dashed]="s.dashed" [style.background]="s.dashed ? null : s.color"
                    [style.color]="s.color"></span>{{ s.label }}</span>
          }
        </div>
      }

      <svg:g marks tabindex="0" (keydown)="onKey($event)" (blur)="hover.set(null)">
        @if (geo().area; as area) {
          <path class="area" [attr.d]="area" [style.fill]="series()[0].color" />
        }
        @for (band of geo().bands; track band.key) {
          <path class="band" [attr.d]="band.d" [style.fill]="band.color" />
        }
        @for (line of geo().lines; track line.key) {
          <path class="line" [class.dashed]="line.dashed" [attr.d]="line.d" [style.stroke]="line.color" />
        }
        @for (end of geo().ends; track end.key) {
          <circle class="dot" [attr.cx]="end.x" [attr.cy]="end.y" r="4" [style.fill]="end.color" />
          @if (end.showLabel) {
            <text class="end-label" [attr.x]="end.x + 8" [attr.y]="end.y" dominant-baseline="middle">{{ end.label }}</text>
          }
        }
        @if (hoverInfo(); as h) {
          <line class="crosshair" [attr.x1]="h.x" [attr.x2]="h.x" [attr.y1]="m.top" [attr.y2]="height() - m.bottom" />
          @for (d of h.dots; track d.key) {
            <circle class="dot" [attr.cx]="h.x" [attr.cy]="d.y" r="4" [style.fill]="d.color" />
          }
        }
        <rect class="hit" [attr.x]="m.left" [attr.y]="m.top" [attr.width]="width() - m.left - m.right"
              [attr.height]="height() - m.top - m.bottom"
              (pointermove)="onPointer($event)" (pointerleave)="hover.set(null)" />
      </svg:g>

      @if (hoverInfo(); as h) {
        <div overlay class="chart-tooltip" [style.left.px]="h.tipX" [style.top.px]="m.top">
          <div class="tt-title">{{ h.title }}</div>
          @for (row of h.rows; track row.key) {
            <div class="tt-row"><span class="tt-key" [style.background]="row.color"></span>
              <span class="tt-value">{{ row.value }}</span>
              @if (row.range) { <span class="tt-range">{{ row.range }}</span> }
              <span class="tt-label">{{ row.label }}</span></div>
          }
        </div>
      }

      <table table class="data">
        <thead><tr><th>{{ granularity() === 'hour' ? 'Hour' : 'Day' }}</th>
          @for (s of series(); track s.key) { <th class="r">{{ s.label }}</th> }</tr></thead>
        <tbody>
          @for (row of tableRows(); track row.t) {
            <tr><td>{{ row.label }}</td>
              @for (cell of row.cells; track $index) { <td class="r">{{ cell }}</td> }</tr>
          }
        </tbody>
      </table>
    </app-chart-frame>
  `,
  styles: `
    :host { display: block; }
    g:focus { outline: none; }
    .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
    .area, .band { opacity: 0.1; }
    .line.dashed { stroke-dasharray: 5 4; }
    .key-dashed { width: 18px !important; height: 0 !important; border-top: 2px dashed currentColor; }
    .tt-range { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
    .dot { stroke: var(--surface-1); stroke-width: 2; }
    .end-label { fill: var(--text-primary); font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
    .crosshair { stroke: var(--text-muted); stroke-width: 1; }
    .hit { fill: transparent; cursor: crosshair; }
  `,
})
export class LineChartComponent {
  readonly series = input.required<Series[]>();
  readonly granularity = input<'day' | 'hour'>('day');
  readonly height = input(260);
  readonly ariaLabel = input('Line chart');
  readonly fmt = input<(v: number | null) => string>(formatPlat);
  /** Shade the area under the first series (only sensible for a single series). */
  readonly fill = input(false);

  protected readonly m = M;
  protected readonly width = injectElementWidth();
  protected readonly hover = signal<number | null>(null);

  /** Sorted union of timestamps across series. */
  private readonly times = computed(() =>
    [...new Set(this.series().flatMap((s) => [...s.points.map((p) => p.t), ...(s.range ?? []).map((r) => r.t)]))]
      .sort((a, b) => a - b),
  );

  private readonly scales = computed(() => {
    const values = this.series()
      .flatMap((s) => [...s.points.map((p) => p.v), ...(s.range ?? []).flatMap((r) => [r.low, r.high])])
      .filter((v): v is number => v !== null);
    const times = this.times();
    const ticks = niceTicks(Math.min(...values, Infinity), Math.max(...values, -Infinity), 4);
    const x = linear([times[0] ?? 0, times[times.length - 1] ?? 1], [M.left, this.width() - M.right]);
    const y = linear([ticks[0], ticks[ticks.length - 1]], [this.height() - M.bottom, M.top]);
    return { x, y, ticks };
  });

  private readonly fmtTime = computed(() => (this.granularity() === 'hour' ? formatHour : formatDay));

  protected readonly geo = computed(() => {
    const { x, y, ticks } = this.scales();
    const times = this.times();
    const lines = this.series().map((s) => ({ key: s.key, color: s.color, dashed: !!s.dashed, d: linePath(s.points, x, y) }));
    const bands = this.series()
      .filter((s) => s.range?.length)
      .map((s) => ({ key: s.key, color: s.color, d: bandPath(s.range!, x, y) }));

    const baseline = this.height() - M.bottom;
    const first = this.series()[0];
    const area = this.fill() && first && lines[0]?.d && !first.points.some((p) => p.v === null)
      ? `${lines[0].d}L${x(first.points[first.points.length - 1].t)},${baseline}L${x(first.points[0].t)},${baseline}Z`
      : null;

    // End labels: drop a label that would collide with one already placed (the legend still names it).
    // A line continued by a dashed projection (e.g. a forecast) leaves the label to the projection's end.
    const continued = new Set(this.series().filter((s) => s.dashed && s.points.length).map((s) => `${s.color}@${s.points[0].t}`));
    const placed: number[] = [];
    const ends = this.series().flatMap((s) => {
      const last = [...s.points].reverse().find((p) => p.v !== null);
      if (!last || last.v === null || (!s.dashed && continued.has(`${s.color}@${last.t}`))) return [];
      const ey = y(last.v);
      const showLabel = placed.every((py) => Math.abs(py - ey) > 14);
      if (showLabel) placed.push(ey);
      return [{ key: s.key, color: s.color, x: x(last.t), y: ey, label: this.fmt()(last.v), showLabel }];
    });

    const maxLabels = Math.max(2, Math.floor((this.width() - M.left - M.right) / 80));
    return {
      lines,
      bands,
      area,
      ends,
      yTicks: ticks.map((v) => ({ v, y: y(v), label: formatTick(v) })),
      xTicks: spacedIndexes(times.length, maxLabels).map((i) => ({ t: times[i], x: x(times[i]), label: this.fmtTime()(times[i]) })),
    };
  });

  protected readonly hoverInfo = computed(() => {
    const index = this.hover();
    const times = this.times();
    if (index === null || !times.length) return null;
    const t = times[index];
    const { x, y } = this.scales();
    const px = x(t);
    const rows = this.series().map((s) => {
      const v = s.points.find((pt) => pt.t === t)?.v ?? null;
      return { key: s.key, color: s.color, label: s.label, value: this.fmt()(v), v, range: this.rangeText(s, t) };
    });
    return {
      x: px,
      tipX: tooltipLeft(px, this.width()),
      title: this.granularity() === 'hour' ? `${formatDay(t)}, ${formatHour(t)}` : formatDay(t),
      rows,
      dots: rows.filter((r) => r.v !== null).map((r) => ({ key: r.key, color: r.color, y: y(r.v as number) })),
    };
  });

  protected readonly tableRows = computed(() => {
    const label = this.granularity() === 'hour' ? (t: number) => `${formatDay(t)} ${formatHour(t)}` : formatDay;
    return this.times().slice().reverse().map((t) => ({
      t,
      label: label(t),
      cells: this.series().map((s) => {
        const v = s.points.find((p) => p.t === t)?.v ?? null;
        const range = this.rangeText(s, t);
        return range ? `${this.fmt()(v)} ${range}` : this.fmt()(v);
      }),
    }));
  });

  /** "(105p–135p)" when the series has a range at t that is wider than a point. */
  private rangeText(s: Series, t: number): string | null {
    const r: RangePoint | undefined = s.range?.find((p) => p.t === t);
    return r && r.high > r.low ? `(${this.fmt()(r.low)}–${this.fmt()(r.high)})` : null;
  }

  protected onPointer(event: PointerEvent): void {
    const svg = (event.currentTarget as SVGElement).ownerSVGElement!;
    const times = this.times();
    if (!times.length) return;
    const px = event.clientX - svg.getBoundingClientRect().left;
    const { x } = this.scales();
    const [t0, t1] = [times[0], times[times.length - 1]];
    this.hover.set(nearestIndex(times, t0 + ((px - x(t0)) / (x(t1) - x(t0) || 1)) * (t1 - t0)));
  }

  protected onKey(event: KeyboardEvent): void {
    const last = this.times().length - 1;
    if (last < 0) return;
    const current = this.hover() ?? last;
    if (event.key === 'ArrowLeft') this.hover.set(Math.max(0, current - 1));
    else if (event.key === 'ArrowRight') this.hover.set(Math.min(last, current + 1));
    else return;
    event.preventDefault();
  }
}

function formatTick(v: number): string {
  return Math.abs(v) >= 10_000 ? `${Math.round(v / 1000)}K` : v.toLocaleString('en-US');
}
