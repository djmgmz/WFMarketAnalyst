import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/** Line icons drawn on a 20px grid with one stroke weight; the angular joins echo the bevelled panels. */
const PATHS = {
  overview: 'M3 3h6v6H3zM11 3h6v4h-6zM11 9h6v8h-6zM3 11h6v6H3z',
  items: 'M3 5h2M8 5h9M3 10h2M8 10h9M3 15h2M8 15h9',
  ducats: 'M10 2.5 16.5 10 10 17.5 3.5 10zM10 6.5 13 10l-3 3.5L7 10z',
  forecast: 'M2.5 15.5l4-5 3 2.5 3.5-5M15 6l2.5-2.5M13 8l1-1',
  watchlist: 'M10 2.8l2.2 4.6 5 .6-3.7 3.4 1 5-4.5-2.6-4.5 2.6 1-5L2.8 8l5-.6z',
  running: 'M10 4.5 15.5 10 10 15.5 4.5 10z',
  warning: 'M10 3 18 17H2zM10 8v4M10 14.2v.1',
  check: 'M4 10.5l4 4 8-9',
} as const;

export type IconName = keyof typeof PATHS;

@Component({
  selector: 'app-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg [attr.width]="size()" [attr.height]="size()" viewBox="0 0 20 20" fill="none" stroke="currentColor"
         stroke-width="1.5" stroke-linejoin="miter" stroke-linecap="square" aria-hidden="true" focusable="false">
      <path [attr.d]="d()" />
    </svg>
  `,
  styles: `:host { display: inline-flex; flex: none; } svg { display: block; }`,
})
export class IconComponent {
  readonly name = input.required<IconName>();
  readonly size = input(18);
  protected readonly d = computed(() => PATHS[this.name()]);
}
