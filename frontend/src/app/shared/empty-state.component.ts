import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** Placeholder for views whose data has not been recorded yet. */
@Component({
  selector: 'app-empty-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="title">{{ title() }}</div>
    <div class="body"><ng-content /></div>
  `,
  styles: `
    :host { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
            min-height: 140px; padding: 20px; border: 1px dashed var(--axis); border-radius: var(--r-panel);
            corner-shape: var(--corner); text-align: center; }
    .title { font-family: var(--font-display); font-size: 15px; font-weight: 600; }
    .body { color: var(--text-secondary); font-size: 13px; max-width: 460px; }
  `,
})
export class EmptyStateComponent {
  readonly title = input.required<string>();
}
