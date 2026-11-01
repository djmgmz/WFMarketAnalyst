import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';

import { ApiService } from '../../core/api.service';
import { formatPlat, timeAgo } from '../../core/format';
import { WatchRow } from '../../core/models';
import { EmptyStateComponent } from '../../shared/empty-state.component';
import { IconComponent } from '../../shared/icon.component';
import { ItemThumbComponent } from '../../shared/item-thumb.component';

/** Items the user tracks, with a target price. The recorder snapshots these every 15 minutes. */
@Component({
  selector: 'app-watchlist-page',
  imports: [RouterLink, EmptyStateComponent, ItemThumbComponent, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Watchlist</h1>
          <p>Set a target price; items at or below it are flagged. Best prices are snapshotted while recording.</p>
        </div>
      </header>

      <section class="card">
        @if (rows().length) {
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>Item</th><th class="r">Latest median</th><th class="r">Best sell (snapshot)</th>
                <th class="r">Target</th><th>Status</th><th></th></tr></thead>
              <tbody>
                @for (row of rows(); track row.id) {
                  <tr>
                    <td><a class="item-link" [routerLink]="['/items', row.slug]">
                      <app-item-thumb [image]="row.image" [name]="row.name" />{{ row.name }}</a></td>
                    <td class="r">{{ plat(row.price) }}</td>
                    <td class="r">{{ plat(row.best_sell) }}
                      @if (row.snapshot_at) { <div class="muted small">{{ ago(row.snapshot_at) }}</div> }</td>
                    <td class="r">
                      <input class="input target" type="number" min="1" placeholder="–" [attr.aria-label]="'Target price for ' + row.name"
                             [value]="row.target_price ?? ''" (change)="setTarget(row, $any($event.target).value)" />
                    </td>
                    <td>
                      @switch (status(row)) {
                        @case ('hit') { <span class="status hit"><app-icon name="check" [size]="13" />At or below target</span> }
                        @case ('above') { <span class="status">Above target</span> }
                        @case ('no-target') { <span class="muted">No target set</span> }
                        @default { <span class="muted">No price yet</span> }
                      }
                    </td>
                    <td class="r"><button class="btn ghost small" type="button" (click)="remove(row)">Remove</button></td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        } @else if (list.error()) {
          <app-empty-state title="Could not load the watchlist">Is the backend running on port 8000?</app-empty-state>
        } @else if (!list.isLoading()) {
          <app-empty-state title="Your watchlist is empty">
            Open any item and choose <strong>Add to watchlist</strong>, or <a routerLink="/items">browse items</a>.
          </app-empty-state>
        }
      </section>
    </div>
  `,
  styles: `
    .item-link { gap: 10px; font-weight: 500; }
    .target { min-width: 0; width: 90px; text-align: right; }
    .small { font-size: 11.5px; }
    .status.hit { display: inline-flex; align-items: center; gap: 5px; color: var(--delta-up); font-weight: 600; }
  `,
})
export class WatchlistPage {
  private readonly api = inject(ApiService);

  protected readonly list = rxResource({ stream: () => this.api.watchlist() });
  protected readonly rows = computed(() => this.list.value() ?? []);

  protected status(row: WatchRow): 'hit' | 'above' | 'no-target' | 'no-price' {
    const price = row.best_sell ?? row.price;
    if (row.target_price === null) return 'no-target';
    if (price === null) return 'no-price';
    return price <= row.target_price ? 'hit' : 'above';
  }

  protected setTarget(row: WatchRow, raw: string): void {
    const value = raw ? Math.max(1, Math.round(Number(raw))) : null;
    this.api.watch(row.slug, value).subscribe(() => this.list.reload());
  }

  protected remove(row: WatchRow): void {
    this.api.unwatch(row.slug).subscribe(() => this.list.reload());
  }

  protected readonly plat = formatPlat;
  protected readonly ago = timeAgo;
}
