import { ChangeDetectionStrategy, Component, computed, input, linkedSignal } from '@angular/core';

/** Item image saved by the item-images job (served at /api/images), with a lettered fallback. */
@Component({
  selector: 'app-item-thumb',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (src() && !failed()) {
      <img [src]="src()" [alt]="''" [width]="size()" [height]="size()" loading="lazy" (error)="failed.set(true)" />
    } @else {
      <span class="fallback" [style.width.px]="size()" [style.height.px]="size()">{{ name().charAt(0) }}</span>
    }
  `,
  styles: `
    :host { display: inline-flex; flex: none; }
    img { object-fit: contain; border-radius: var(--r-control); corner-shape: var(--corner); background: var(--surface-2); }
    .fallback { display: inline-flex; align-items: center; justify-content: center; border-radius: var(--r-control);
                corner-shape: var(--corner); border: 1px solid var(--border); background: var(--surface-2);
                color: var(--text-secondary); font-family: var(--font-display); font-weight: 600; }
  `,
})
export class ItemThumbComponent {
  /** File name from the item's `image` field; null until the item-images job has fetched it. */
  readonly image = input<string | null>(null);
  readonly name = input('');
  readonly size = input(28);

  protected readonly src = computed(() => {
    const image = this.image();
    return image ? `/api/images/${encodeURIComponent(image)}` : null;
  });
  /** A broken image falls back to the letter; a new image gets a fresh try. */
  protected readonly failed = linkedSignal({ source: this.src, computation: () => false });
}
