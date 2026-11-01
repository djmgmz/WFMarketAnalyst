import { Injectable, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Subject, catchError, merge, of, switchMap, timer } from 'rxjs';

import { ApiService } from './api.service';
import { Job } from './models';

const POLL_MS = 3000;

/**
 * Status of the background jobs, polled from the local API. Shared by the sidebar indicator, the
 * Overview panel, and pages that refresh themselves while a job is producing data.
 */
@Injectable({ providedIn: 'root' })
export class JobsService {
  private readonly api = inject(ApiService);
  private readonly refresh$ = new Subject<void>();

  readonly jobs = toSignal(
    merge(timer(0, POLL_MS), this.refresh$).pipe(
      switchMap(() => this.api.jobs().pipe(catchError(() => of([] as Job[])))),
    ),
    { initialValue: [] as Job[] },
  );

  /** Wall clock ticking every second, for elapsed times and ETAs. */
  readonly now = signal(Date.now());

  /** Changes whenever a job makes progress or changes state; pages reload their data on it. */
  readonly progressKey = computed(() =>
    this.jobs().map((j) => `${j.name}:${j.run?.state}:${j.run?.done}`).join('|'),
  );

  constructor() {
    setInterval(() => this.now.set(Date.now()), 1000);
  }

  /** Poll now instead of waiting for the next tick (after Start/Stop). */
  refresh(): void {
    this.refresh$.next();
  }
}
