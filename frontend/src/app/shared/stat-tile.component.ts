import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** Stat tile: sentence-case label, proportional-figure value, optional note. */
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
    :host { display: block; background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--r-panel);
            corner-shape: var(--corner); padding: 14px 16px; min-width: 0; }
    .label { color: var(--text-secondary); font-size: 12.5px; }
    .value { font-size: 26px; font-weight: 600; letter-spacing: -0.01em; margin-top: 2px; line-height: 1.2;
              font-variant-numeric: tabular-nums; }
    .note { color: var(--text-muted); font-size: 12px; margin-top: 4px; }
  `,
})
export class StatTileComponent {
  readonly label = input.required<string>();
  readonly value = input.required<string>();
  readonly note = input<string | null>(null);
}
