import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { formatDay, formatHour, formatNumber } from '../../core/format';
import { ChartFrameComponent } from './chart-frame.component';
import { injectElementWidth } from './element-width';
import { CHART_MARGIN as M, Point, columnPath, linear, niceTicks, spacedIndexes, tooltipLeft } from './scale';

/**
 * Single-series columns over time (e.g. trade volume): columns capped at 24px
 * with a 4px rounded data-end and a surface gap; each column is its own hover
 * and focus target.
 */
@Component({
  selector: 'app-column-chart',
  imports: [ChartFrameComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-chart-frame [width]="width()" [height]="height()" [yTicks]="geo().yTicks" [xTicks]="geo().xTicks"
                     [ariaLabel]="ariaLabel()">
      <svg:g marks>
        @for (bar of geo().bars; track bar.t; let i = $index) {
          <g class="bar" [class.active]="hover() === i" tabindex="0" role="img"
             [attr.aria-label]="fmtTime(bar.t) + ': ' + fmtValue(bar.v)"
             (pointerenter)="hover.set(i)" (pointerleave)="hover.set(null)"
             (focus)="hover.set(i)" (blur)="hover.set(null)">
            <rect class="hit" [attr.x]="bar.slotX" [attr.y]="m.top" [attr.width]="bar.slotW"
                  [attr.height]="height() - m.top - m.bottom" />
            <path [attr.d]="bar.d" [style.fill]="color()" />
          </g>
        }
      </svg:g>

      @if (hoverBar(); as b) {
        <div overlay class="chart-tooltip" [style.left.px]="b.tipX" [style.top.px]="m.top">
          <div class="tt-title">{{ fmtTime(b.t) }}</div>
          <div class="tt-row"><span class="tt-key" [style.background]="color()"></span>
            <span class="tt-value">{{ fmtValue(b.v) }}</span><span class="tt-label">{{ label() }}</span></div>
        </div>
      }

      <table table class="data">
        <thead><tr><th>{{ granularity() === 'hour' ? 'Hour' : 'Day' }}</th><th class="r">{{ label() }}</th></tr></thead>
        <tbody>
          @for (p of reversed(); track p.t) {
            <tr><td>{{ fmtTime(p.t) }}</td><td class="r">{{ fmtValue(p.v) }}</td></tr>
          }
        </tbody>
      </table>
    </app-chart-frame>
  `,
  styles: `
    :host { display: block; }
    .hit { fill: transparent; }
    .bar { outline: none; }
    .bar.active path { filter: brightness(1.15); }
    .bar.active .hit, .bar:focus-visible .hit { fill: var(--hover-wash); }
  `,
})
export class ColumnChartComponent {
  readonly points = input.required<Point[]>();
  readonly label = input('Volume');
  readonly color = input('var(--series-1)');
  readonly granularity = input<'day' | 'hour'>('day');
  readonly height = input(180);
  readonly ariaLabel = input('Column chart');

  protected readonly m = M;
  protected readonly width = injectElementWidth();
  protected readonly hover = signal<number | null>(null);

  protected readonly reversed = computed(() => this.points().slice().reverse());

  protected readonly geo = computed(() => {
    const pts = this.points();
    const plotW = this.width() - M.left - M.right;
    const baseline = this.height() - M.bottom;
    const ticks = niceTicks(0, Math.max(1, ...pts.map((p) => p.v ?? 0)), 3);
    const y = linear([0, ticks[ticks.length - 1]], [baseline, M.top]);
    const slotW = plotW / Math.max(1, pts.length);
    const barW = Math.max(1, Math.min(24, slotW - 2)); // 24px cap, 2px surface gap
    const bars = pts.map((p, i) => {
      const slotX = M.left + i * slotW;
      const x = slotX + (slotW - barW) / 2;
      return { t: p.t, v: p.v, slotX, slotW, cx: x + barW / 2, d: columnPath(x, y(p.v ?? 0), barW, baseline) };
    });
    return {
      bars,
      yTicks: ticks.map((v) => ({ v, y: y(v), label: formatNumber(v) })),
      xTicks: spacedIndexes(pts.length, Math.max(2, Math.floor(plotW / 80))).map((i) => ({
        t: pts[i].t, x: bars[i].cx, label: (this.granularity() === 'hour' ? formatHour : formatDay)(pts[i].t),
      })),
    };
  });

  protected readonly hoverBar = computed(() => {
    const bar = this.hover() === null ? undefined : this.geo().bars[this.hover()!];
    return bar ? { ...bar, tipX: tooltipLeft(bar.cx, this.width()) } : null;
  });

  protected fmtTime(t: number): string {
    return this.granularity() === 'hour' ? `${formatDay(t)}, ${formatHour(t)}` : formatDay(t);
  }

  protected fmtValue(v: number | null): string {
    return formatNumber(v);
  }
}
