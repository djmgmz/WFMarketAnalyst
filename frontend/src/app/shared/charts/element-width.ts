import { DestroyRef, ElementRef, Signal, inject, signal } from '@angular/core';

/** The host element's current width (at least `min`), kept up to date while it lives. */
export function injectElementWidth(min = 280): Signal<number> {
  const width = signal(600);
  const el = inject(ElementRef<HTMLElement>).nativeElement as HTMLElement;
  const observer = new ResizeObserver(([entry]) => width.set(Math.max(min, entry.contentRect.width)));
  observer.observe(el);
  inject(DestroyRef).onDestroy(() => observer.disconnect());
  return width.asReadonly();
}
