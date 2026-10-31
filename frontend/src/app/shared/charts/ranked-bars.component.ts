import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { formatNumber } from '../../core/format';

export interface RankedRow {
  key: string;
  label: string;
  value: number;
  /** Optional router link and query params for the row label. */
  link?: string;
  query?: Record<string, string>;
}

/**
 * Ranked horizontal bars: one hue, largest first, value printed at the tip.
 * Every value is direct-labelled, so the bars double as their own table view.
 */
@Component({
  selector: 'app-ranked-bars',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <ol class="rows" [attr.aria-label]="ariaLabel()" [style.--label-width.px]="labelWidth()">
      @for (row of rows(); track row.key) {
        <li class="row">
          @if (row.link) {
            <a class="label" [routerLink]="row.link" [queryParams]="row.query">{{ row.label }}</a>
          } @else {
            <span class="label">{{ row.label }}</span>
          }
          <span class="track">
            <span class="bar" [style.width.%]="row.pct" [style.background]="color()"></span>
            <span class="value">{{ format()(row.value) }}</span>
          </span>
        </li>
      }
    </ol>
  `,
  styles: `
    :host { display: block; }
    .rows { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
    .row { display: grid; grid-template-columns: var(--label-width, 96px) 1fr; align-items: center; gap: 12px; padding: 2px 0; border-radius: var(--r-control); corner-shape: var(--corner); }
    .row:hover { background: var(--hover-wash); }
    .label { color: var(--text-secondary); font-size: 13px; text-decoration: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    a.label:hover { color: var(--text-primary); text-decoration: underline; }
    .track { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .bar { height: 14px; min-width: 2px; border-radius: 0 4px 4px 0; }
    .value { color: var(--text-primary); font-size: 12.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  `,
})
export class RankedBarsComponent {
  readonly data = input.required<RankedRow[]>();
  readonly color = input('var(--series-1)');
  readonly ariaLabel = input('Ranked bars');
  readonly format = input<(v: number) => string>(formatNumber);
  readonly labelWidth = input(96);

  protected readonly rows = computed(() => {
    const sorted = [...this.data()].sort((a, b) => b.value - a.value);
    // Scale to the largest value (values can be fractions, e.g. shares).
    const max = Math.max(0, ...sorted.map((r) => r.value)) || 1;
    // Leave room at the tip for the value label.
    return sorted.map((r) => ({ ...r, pct: (r.value / max) * 82 }));
  });
}
