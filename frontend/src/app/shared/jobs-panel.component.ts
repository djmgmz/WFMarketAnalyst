import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, linkedSignal, signal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { Observable } from 'rxjs';

import { ApiService } from '../core/api.service';
import { STATE_LABELS, describe, progressFraction } from '../core/job-format';
import { JobsService } from '../core/jobs.service';
import { Job, JobName, JobStartOptions } from '../core/models';
import { IconComponent } from './icon.component';

/** Live status of every job, with Start / Stop controls. Driven entirely by the job definitions. */
@Component({
  selector: 'app-jobs-panel',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="jobs">
      @for (card of cards(); track card.job.name) {
        <article class="job" [attr.aria-label]="card.job.title">
          <header>
            <span class="title">{{ card.job.title }}</span>
            @if (card.job.run; as run) {
              <span class="state" [attr.data-state]="run.state">
                <app-icon class="icon" [name]="states[run.state].icon" [size]="13" />{{ states[run.state].label }}
              </span>
            } @else {
              <span class="state muted">Not run yet</span>
            }
          </header>

          @if (card.text; as text) {
            @if (card.fraction !== null) {
              <div class="meter" role="progressbar" [attr.aria-valuenow]="card.job.run?.done" aria-valuemin="0"
                   [attr.aria-valuemax]="card.job.run?.total" [attr.aria-label]="card.job.title + ' progress'">
                <span class="fill" [style.width.%]="card.fraction * 100"></span>
              </div>
            }
            <p class="line">{{ text.summary }}</p>
            @if (text.sub) { <p class="sub">{{ text.sub }}</p> }
          } @else {
            <p class="sub">Or from a terminal: <code>{{ card.job.command }}</code></p>
          }

          <footer class="controls">
            @switch (card.job.run?.state) {
              @case ('running') {
                <button class="btn small" type="button" (click)="stop(card.job)"
                        [disabled]="busy() === card.job.name || card.job.run?.stop_requested">
                  {{ card.job.run?.stop_requested ? 'Stopping…' : 'Stop' }}</button>
              }
              @case ('stalled') {
                <button class="btn small" type="button" (click)="stop(card.job)" [disabled]="busy() === card.job.name">Clear</button>
              }
              @default {
                @if (card.job.scoped) {
                  <select class="select scope" aria-label="What to cover" (change)="scope.set($any($event.target).value)">
                    <option value="history" [selected]="scope() === 'history'">Items with history (daily refresh)</option>
                    <option value="watchlist" [selected]="scope() === 'watchlist'">Watchlist items</option>
                    @for (c of categories(); track c.category) {
                      <option [value]="c.category" [selected]="scope() === c.category">{{ c.category }} ({{ c.count }} items)</option>
                    }
                  </select>
                }
                <button class="btn small" type="button" (click)="start(card.job)" [disabled]="busy() === card.job.name">
                  {{ busy() === card.job.name ? 'Starting…' : card.startLabel }}</button>
                @if (card.job.resumable) { <span class="hint">Picks up where it stopped</span> }
              }
            }
          </footer>
          @if (errors()[card.job.name]; as error) { <p class="error" role="alert">{{ error }}</p> }
        </article>
      }
    </div>
  `,
  styles: `
    .jobs { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
    .job { border: 1px solid var(--border); border-radius: var(--r-panel); corner-shape: var(--corner); padding: 12px 14px;
            background: var(--surface-1); min-width: 0; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
    .title { font-family: var(--font-display); font-size: 15px; font-weight: 600; }
    .state { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; color: var(--text-secondary); }
    .state[data-state='running'] .icon { color: var(--accent); }
    .state[data-state='finished'] .icon { color: var(--status-good); }
    .state[data-state='stalled'] .icon { color: var(--status-warning); }
    .state[data-state='failed'] .icon { color: var(--status-critical); }
    .state[data-state='stopped'] .icon { color: var(--text-muted); }
    .meter { height: 6px; background: var(--meter-track); overflow: hidden; margin-bottom: 8px; }
    .fill { display: block; height: 100%; background: var(--accent); transition: width 0.4s cubic-bezier(0.16, 1, 0.3, 1); }
    .line { margin: 0; font-variant-numeric: tabular-nums; }
    .sub { margin: 2px 0 0; color: var(--text-muted); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .controls { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
    .scope { height: 28px; font-size: 12.5px; min-width: 0; }
    .hint { color: var(--text-muted); font-size: 12px; }
    .error { margin: 6px 0 0; color: var(--status-critical); font-size: 12.5px; }
  `,
})
export class JobsPanelComponent {
  private readonly service = inject(JobsService);
  private readonly api = inject(ApiService);
  protected readonly states = STATE_LABELS;

  /** The scope of the last scoped run, so Resume continues that run rather than starting another. */
  private readonly lastScope = computed(() => {
    const label = this.service.jobs().find((job) => job.scoped)?.run?.detail.scope;
    return label === 'items with history' ? 'history' : label === 'watchlist' ? 'watchlist'
      : label && this.categories().some((c) => c.category === label) ? label : 'Arcane';
  });
  /** Scope for jobs that need one: 'history', 'watchlist' or a category name. Follows the last run until changed. */
  protected readonly scope = linkedSignal(() => this.lastScope());
  protected readonly busy = signal<JobName | null>(null);
  protected readonly errors = signal<Partial<Record<JobName, string>>>({});
  private readonly categoryList = rxResource({ stream: () => this.api.categories() });
  protected readonly categories = computed(() => (this.categoryList.value() ?? []).filter((c) => c.count > 0));

  protected readonly cards = computed(() => {
    const now = this.service.now();
    return this.service.jobs().map((job) => ({
      job,
      text: job.run ? describe({ ...job, run: job.run }, now) : null,
      fraction: job.run?.state === 'running' ? progressFraction(job.run) : null,
      startLabel: job.resumable && (job.run?.state === 'stopped' || job.run?.state === 'failed') ? 'Resume' : 'Start',
    }));
  });

  protected start(job: Job): void {
    const scope = this.scope();
    const options: JobStartOptions = !job.scoped ? {}
      : scope === 'watchlist' ? { watchlist: true }
      : scope === 'history' ? { with_history: true }
      : { category: scope };
    this.run(job.name, this.api.startJob(job.name, options));
  }

  protected stop(job: Job): void {
    this.run(job.name, this.api.stopJob(job.name));
  }

  private run(name: JobName, request: Observable<unknown>): void {
    this.busy.set(name);
    this.errors.update((e) => ({ ...e, [name]: undefined }));
    request.subscribe({
      next: () => {
        this.busy.set(null);
        this.service.refresh();
      },
      error: (err: HttpErrorResponse) => {
        this.busy.set(null);
        const detail = typeof err.error?.detail === 'string' ? err.error.detail : err.message;
        this.errors.update((e) => ({ ...e, [name]: detail }));
        this.service.refresh();
      },
    });
  }
}
