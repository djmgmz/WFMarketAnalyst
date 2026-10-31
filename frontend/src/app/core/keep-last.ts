import { Signal, linkedSignal } from '@angular/core';

/**
 * Holds the last defined value of a resource while it refetches, so views keep
 * their frame (at reduced opacity) instead of flashing empty.
 */
export function keepLast<T>(source: Signal<T | undefined>): Signal<T | undefined> {
  return linkedSignal<T | undefined, T | undefined>({
    source,
    computation: (value, previous) => value ?? previous?.value,
  });
}
