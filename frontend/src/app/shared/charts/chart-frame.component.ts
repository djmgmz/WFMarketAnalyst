import { ChangeDetectionStrategy, Component, input, signal } from '@angular/core';

import { CHART_MARGIN, XTick, YTick } from './scale';

/**
 * Everything around a chart's data: legend slot, Chart/Table toggle (every chart
 * has a table twin), hairline grid, axis and tick labels.
 *
 * Charts project their parts:
 *   [legend]   shown above the plot (only for 2+ series)
 *   [marks]    an <svg:g> drawn on top of the grid
 *   [overlay]  HTML over the plot, e.g. the tooltip
 *   [table]    the table view
 */
@Component({
  selector: 'app-chart-frame',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="chart-top">
      <div class="legend-slot"><ng-content select="[legend]" /></div>
      <button class="btn ghost small" type="button" (click)="showTable.set(!showTable())"
              [attr.aria-pressed]="showTable()">{{ showTable() ? 'Chart' : 'Table' }}</button>
    </div>
    @if (showTable()) {
      <div class="table-wrap table-view"><ng-content select="[table]" /></div>
    } @else {
      <div class="plot" [style.height.px]="height()">
        <svg [attr.width]="width()" [attr.height]="height()" role="img" [attr.aria-label]="ariaLabel()">
          @for (tick of yTicks(); track tick.v) {
            <line class="grid" [attr.x1]="m.left" [attr.x2]="width() - m.right" [attr.y1]="tick.y" [attr.y2]="tick.y" />
            <text class="tick" [attr.x]="m.left - 8" [attr.y]="tick.y" text-anchor="end" dominant-baseline="middle">{{ tick.label }}</text>
          }
          <line class="axis" [attr.x1]="m.left" [attr.x2]="width() - m.right"
                [attr.y1]="height() - m.bottom" [attr.y2]="height() - m.bottom" />
          @for (tick of xTicks(); track tick.t) {
            <text class="tick" [attr.x]="tick.x" [attr.y]="height() - m.bottom + 18" text-anchor="middle">{{ tick.label }}</text>
          }
          <ng-content select="[marks]" />
        </svg>
        <ng-content select="[overlay]" />
      </div>
    }
  `,
  styles: `
    :host { display: block; }
    .chart-top { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; min-height: 28px; }
    .legend-slot { flex: 1; min-width: 0; }
    .plot { position: relative; }
    svg { display: block; overflow: visible; }
    .grid { stroke: var(--grid); stroke-width: 1; }
    .axis { stroke: var(--axis); stroke-width: 1; }
    .tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    .table-view { max-height: 320px; overflow-y: auto; }
  `,
})
export class ChartFrameComponent {
  readonly width = input.required<number>();
  readonly height = input.required<number>();
  readonly yTicks = input.required<YTick[]>();
  readonly xTicks = input.required<XTick[]>();
  readonly ariaLabel = input('Chart');

  protected readonly m = CHART_MARGIN;
  protected readonly showTable = signal(false);
}
