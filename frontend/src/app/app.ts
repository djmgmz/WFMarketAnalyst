import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { isActive, pill } from './core/job-format';
import { JobsService } from './core/jobs.service';
import { IconComponent, IconName } from './shared/icon.component';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, IconComponent],
  templateUrl: './app.html',
  styleUrl: './app.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class App {
  private readonly jobs = inject(JobsService);

  protected readonly nav: { path: string; label: string; icon: IconName }[] = [
    { path: '/overview', label: 'Overview', icon: 'overview' },
    { path: '/items', label: 'Items', icon: 'items' },
    { path: '/ducats', label: 'Ducats', icon: 'ducats' },
    { path: '/forecast', label: 'Forecast', icon: 'forecast' },
    { path: '/watchlist', label: 'Watchlist', icon: 'watchlist' },
  ];

  /** Compact status of running (or crashed) jobs, shown on every page. */
  protected readonly jobPills = computed(() =>
    this.jobs.jobs()
      .filter(isActive)
      .map((j) => ({ name: j.name, stalled: j.run.state === 'stalled', label: pill(j) })),
  );
}
