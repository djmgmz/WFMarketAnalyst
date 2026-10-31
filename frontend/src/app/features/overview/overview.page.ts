import { ChangeDetectionStrategy, Component, computed, effect, inject, untracked } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';

import { ApiService } from '../../core/api.service';
import { formatNumber, formatPercent, formatPlat, timeAgo } from '../../core/format';
import { JobsService } from '../../core/jobs.service';
import { RankedBarsComponent, RankedRow } from '../../shared/charts/ranked-bars.component';
import { EmptyStateComponent } from '../../shared/empty-state.component';
import { ItemThumbComponent } from '../../shared/item-thumb.component';
import { JobsPanelComponent } from '../../shared/jobs-panel.component';
import { StatTileComponent } from '../../shared/stat-tile.component';

@Component({
  selector: 'app-overview-page',
  imports: [RouterLink, StatTileComponent, RankedBarsComponent, EmptyStateComponent, ItemThumbComponent, JobsPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Market overview</h1>
          <p>Catalog and recording status for warframe.market (PC, crossplay).</p>
        </div>
        <button class="btn" type="button" (click)="reload()">Refresh</button>
      </header>

      @if (overview.error()) {
        <app-empty-state title="Backend not reachable">
          Start the API with <code>python -m wfm serve</code> in <code>backend/</code>.
        </app-empty-state>
      } @else if (ov(); as o) {
        <section class="kpi-row" aria-label="Key figures">
          <app-stat-tile label="Tradable items" [value]="num(o.catalog.items)"
                         [note]="'List synced ' + ago(o.catalog.listSyncedAt)" />
          <app-stat-tile label="Item details synced" [value]="num(o.catalog.detailsSynced)"
                         [note]="detailNote()" />
          <app-stat-tile label="Prime parts with ducats" [value]="num(o.catalog.primeParts)" />
          <app-stat-tile label="Vaulted relics" [value]="num(o.catalog.relicsVaulted)"
                         [note]="'of ' + num(o.catalog.relicsTotal) + ' relics'" />
          <app-stat-tile label="Orders recorded" [value]="num(o.recording.ordersTotal)"
                         [note]="o.recording.ordersTotal ? num(o.recording.orders24h) + ' in the last 24h' : 'Recording not started'" />
          <app-stat-tile label="Items with price history" [value]="num(o.recording.itemsWithHistory)"
                         [note]="o.recording.statsBackfilledAt ? 'Backfilled ' + ago(o.recording.statsBackfilledAt) : 'No backfill yet'" />
        </section>

        <section class="card">
          <div class="card-header">
            <div><h2>Background jobs</h2><p>Live status of the command-line jobs. This page refreshes itself while they run.</p></div>
          </div>
          <app-jobs-panel />
        </section>

        <div class="grid grid-2">
          <section class="card">
            <div class="card-header">
              <div><h2>Catalog by category</h2><p>Tradable items per category. Select one to browse it.</p></div>
            </div>
            <app-ranked-bars [data]="categoryRows()" ariaLabel="Items per category" />
          </section>

          <section class="card">
            <div class="card-header">
              <div><h2>Biggest movers</h2><p>Latest daily median trade price vs 7 days earlier.</p></div>
            </div>
            @if (moverRows().length) {
              <table class="data">
                <thead><tr><th>Item</th><th class="r">Was</th><th class="r">Now</th><th class="r">Change</th></tr></thead>
                <tbody>
                  @for (m of moverRows(); track m.key) {
                    <tr>
                      <td><a class="item-link" [routerLink]="['/items', m.slug]">
                        <app-item-thumb [image]="m.image" [name]="m.name" [size]="22" />{{ m.name }}</a>
                        @if (m.rank !== null) { <span class="muted"> · rank {{ m.rank }}</span> }</td>
                      <td class="r">{{ plat(m.from) }}</td>
                      <td class="r">{{ plat(m.to) }}</td>
                      <td class="r" [class.delta-up]="m.change > 0" [class.delta-down]="m.change < 0">
                        {{ m.change > 0 ? '▲' : '▼' }} {{ pct(m.change) }}</td>
                    </tr>
                  }
                </tbody>
              </table>
            } @else {
              <app-empty-state title="No price history yet">
                Movers appear once history is backfilled, for example
                <code>python -m wfm backfill --category Arcane</code>.
              </app-empty-state>
            }
          </section>
        </div>

        <section class="card">
          <div class="card-header">
            <div><h2>Predicted movers</h2><p>Largest expected changes in the average price over the next 7 days (model forecasts only).</p></div>
            <a class="muted" routerLink="/forecast">How reliable?</a>
          </div>
          @if (predictedRows().length) {
            <table class="data">
              <thead><tr><th>Item</th><th class="r">Now</th><th class="r">Next 7 days (avg)</th><th class="r">Likely range</th><th class="r">Change</th></tr></thead>
              <tbody>
                @for (m of predictedRows(); track m.key) {
                  <tr>
                    <td><a class="item-link" [routerLink]="['/items', m.item.slug]">
                      <app-item-thumb [image]="m.item.image" [name]="m.item.name" [size]="22" />{{ m.item.name }}</a>
                      @if (m.rank !== null) { <span class="muted"> · rank {{ m.rank }}</span> }</td>
                    <td class="r">{{ plat(m.base_price) }}</td>
                    <td class="r">{{ plat(m.predicted) }}</td>
                    <td class="r muted">{{ plat(m.low) }}–{{ plat(m.high) }}</td>
                    <td class="r" [class.delta-up]="m.change > 0" [class.delta-down]="m.change < 0">
                      {{ m.change > 0 ? '▲' : '▼' }} {{ pct(m.change) }}</td>
                  </tr>
                }
              </tbody>
            </table>
          } @else {
            <app-empty-state title="No model forecasts yet">
              Either the forecast hasn't been trained, or the model doesn't beat "no change" for any group of items yet.
              See the <a routerLink="/forecast">Forecast</a> page.
            </app-empty-state>
          }
        </section>

        <section class="card">
          <div class="card-header">
            <div><h2>Live order feed</h2><p>Newest orders captured from the WebSocket feed.</p></div>
            @if (o.recording.lastOrderAt) { <span class="muted">Last order {{ ago(o.recording.lastOrderAt) }}</span> }
          </div>
          @if (recent.value()?.length) {
            <div class="table-wrap">
              <table class="data">
                <thead><tr><th>Item</th><th>Type</th><th class="r">Price</th><th class="r">Qty</th>
                  <th>Trader</th><th class="r">Posted</th></tr></thead>
                <tbody>
                  @for (order of recent.value(); track order.id) {
                    <tr>
                      <td><a class="item-link" [routerLink]="['/items', order.slug]">
                        <app-item-thumb [image]="order.image" [name]="order.name" [size]="22" />{{ order.name }}</a></td>
                      <td><span class="chip">{{ order.type === 'sell' ? 'Selling' : 'Buying' }}</span></td>
                      <td class="r">{{ plat(order.platinum) }}</td>
                      <td class="r">{{ order.quantity }}</td>
                      <td class="secondary">{{ order.user_name }}</td>
                      <td class="r muted">{{ ago(order.created_at) }}</td>
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          } @else {
            <app-empty-state title="Nothing recorded yet">
              Start the recorder with <code>python -m wfm record</code> to capture every new order as it is posted.
            </app-empty-state>
          }
        </section>
      }
    </div>
  `,
  styles: `
    .item-link { display: inline-flex; align-items: center; gap: 8px; text-decoration: none; }
    .item-link:hover { text-decoration: underline; }
  `,
})
export class OverviewPage {
  private readonly api = inject(ApiService);
  private readonly jobs = inject(JobsService);

  protected readonly overview = rxResource({ stream: () => this.api.overview() });
  protected readonly recent = rxResource({ stream: () => this.api.recentOrders(15) });
  protected readonly movers = rxResource({ stream: () => this.api.movers(7) });
  protected readonly predicted = rxResource({ stream: () => this.api.forecastMovers(7) });
  protected readonly ov = computed(() => this.overview.value());

  protected readonly categoryRows = computed<RankedRow[]>(() =>
    (this.ov()?.catalog.byCategory ?? [])
      .filter((c) => c.count > 0)
      .map((c) => ({ key: c.category, label: c.category, value: c.count, link: '/items', query: { category: c.category } })),
  );

  protected readonly moverRows = computed(() => {
    const m = this.movers.value();
    if (!m) return [];
    return [...m.gainers.slice(0, 4), ...m.losers.slice(0, 4)]
      .map((r) => ({ key: `${r.item.slug}-${r.rank}`, slug: r.item.slug, name: r.item.name, image: r.item.image, ...r }));
  });

  protected readonly predictedRows = computed(() => {
    const m = this.predicted.value();
    if (!m) return [];
    return [...m.gainers.slice(0, 4), ...m.losers.slice(0, 4)].map((r) => ({ key: `${r.item.slug}-${r.rank}`, ...r }));
  });

  protected readonly detailNote = computed(() => {
    const c = this.ov()?.catalog;
    if (!c || !c.items) return null;
    return c.detailsSynced >= c.items ? 'Complete' : `${Math.round((c.detailsSynced / c.items) * 100)}% complete`;
  });

  constructor() {
    // New orders, prices or item details landed: refresh the figures that depend on them.
    let first = true;
    effect(() => {
      this.jobs.progressKey();
      if (first) {
        first = false;
        return;
      }
      untracked(() => this.reload());
    });
  }

  protected reload(): void {
    this.overview.reload();
    this.recent.reload();
    this.movers.reload();
    this.predicted.reload();
  }

  protected readonly num = formatNumber;
  protected readonly plat = formatPlat;
  protected readonly pct = (v: number) => formatPercent(v);
  protected readonly ago = timeAgo;
}
