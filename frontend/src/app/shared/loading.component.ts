import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/** Placeholder rows while a panel's data loads, so loading never reads as "nothing recorded". */
@Component({
  selector: 'app-loading',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { role: 'status', 'aria-label': 'Loading' },
  template: `@for (row of rowList(); track $index) { <span class="row"></span> }`,
  styles: `
    :host { display: grid; gap: 12px; padding: 6px 0; }
    .row { height: 12px; background: var(--surface-2); animation: pulse 1.4s ease-in-out infinite; }
    .row:nth-child(3n + 2) { width: 82%; }
    .row:nth-child(3n) { width: 64%; }
    @keyframes pulse { 50% { opacity: 0.45; } }
    @media (prefers-reduced-motion: reduce) { .row { animation: none; } }
  `,
})
export class LoadingComponent {
  readonly rows = input(4);
  protected readonly rowList = computed(() => Array.from({ length: this.rows() }));
}
