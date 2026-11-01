import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** Stat tile: sentence-case label, tabular value, optional note. A cell of a `.kpi-row` panel. */
@Component({
  selector: 'app-stat-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="label">{{ label() }}</div>
    <div class="value">{{ value() }}</div>
    @if (note()) {
      <div class="note">{{ note() }}</div>
    }
    <ng-content />
  `,
  styles: `
    :host { display: block; padding: 14px 18px; min-width: 0; margin: -1px 0 0 -1px;
            border-left: 1px solid var(--border); border-top: 1px solid var(--border); }
    .label { color: var(--text-secondary); font-size: 12.5px; }
    .value { font-size: 22px; font-weight: 600; letter-spacing: -0.01em; margin-top: 2px; line-height: 1.2;
              font-variant-numeric: tabular-nums; }
    .note { color: var(--text-muted); font-size: 12px; margin-top: 4px; }
  `,
})
export class StatTileComponent {
  readonly label = input.required<string>();
  readonly value = input.required<string>();
  readonly note = input<string | null>(null);
}
