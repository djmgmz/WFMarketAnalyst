import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, signal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { NgTemplateOutlet } from '@angular/common';
import { RouterLink } from '@angular/router';

import { ApiService } from '../../core/api.service';
import { formatCredits, formatDateTime, formatDay, formatNumber, formatPlat, timeAgo, titleCase } from '../../core/format';
import { keepLast } from '../../core/keep-last';
import { Granularity, LiveOrder } from '../../core/models';
import { ColumnChartComponent } from '../../shared/charts/column-chart.component';
import { LineChartComponent } from '../../shared/charts/line-chart.component';
import { Series } from '../../shared/charts/scale';
import { EmptyStateComponent } from '../../shared/empty-state.component';
import { IconComponent } from '../../shared/icon.component';
import { ItemThumbComponent } from '../../shared/item-thumb.component';

@Component({
  selector: 'app-item-detail-page',
  imports: [NgTemplateOutlet, RouterLink, LineChartComponent, ColumnChartComponent, EmptyStateComponent, ItemThumbComponent,
            IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="page">
      <a class="back muted" routerLink="/items"><app-icon name="back" [size]="14" />All items</a>

      @if (item.error()) {
        <app-empty-state title="Item not found">No item with the slug <code>{{ slug() }}</code> is in the catalog.</app-empty-state>
      } @else if (item.value(); as it) {
        <header class="hero">
          <app-item-thumb [image]="it.image" [name]="it.name" [size]="72" />
          <div class="hero-text">
            <h1>{{ it.name }}</h1>
            <div class="chips">
              <span class="chip">{{ it.category }}</span>
              @if (it.vaulted) { <span class="chip">Vaulted</span> }
              @for (tag of chipTags(); track tag) { <span class="chip">{{ title(tag) }}</span> }
            </div>
            @if (it.description) { <p class="description">{{ it.description }}</p> }
          </div>
          <div class="hero-actions">
            @if (it.watch) {
              <button class="btn" type="button" (click)="unwatch()"><app-icon class="filled" name="watchlist" [size]="15" />Watching</button>
            } @else {
              <button class="btn primary" type="button" (click)="watch()"><app-icon name="watchlist" [size]="15" />Add to watchlist</button>
            }
            @if (it.wiki_link) { <a class="btn ghost" [href]="it.wiki_link" target="_blank" rel="noopener" title="Opens the Warframe wiki in a new tab">Wiki<app-icon name="external" [size]="13" /></a> }
          </div>
        </header>

        <section class="facts card" aria-label="Item facts">
          @for (fact of facts(); track fact.label) {
            <div class="fact"><div class="fact-label">{{ fact.label }}</div><div class="fact-value">{{ fact.value }}</div></div>
          }
        </section>

        <div class="filter-row" aria-label="Chart filters">
          <div class="segmented" role="group" aria-label="Time range">
            <button type="button" [attr.aria-pressed]="granularity() === 'day'" (click)="granularity.set('day')">90 days</button>
            <button type="button" [attr.aria-pressed]="granularity() === 'hour'" (click)="granularity.set('hour')">48 hours</button>
          </div>
          @if (it.max_rank !== null) {
            <div class="segmented" role="group" aria-label="Rank">
              <button type="button" [attr.aria-pressed]="rank() === 0" (click)="rank.set(0)">Rank 0</button>
              <button type="button" [attr.aria-pressed]="rank() === it.max_rank" (click)="rank.set(it.max_rank)">Max rank ({{ it.max_rank }})</button>
            </div>
          }
        </div>

        <div class="grid charts" [class.refetching]="history.isLoading()">
          <section class="card">
            <div class="card-header">
              <div><h2>Price</h2><p>Median completed trade vs what sellers ask, in platinum{{ forecastNote() }}.</p></div>
            </div>
            @if (priceSeries().length) {
              <app-line-chart [series]="priceSeries()" [granularity]="granularity()" ariaLabel="Price history" />
              @if (excludedNote(); as note) {
                <p class="excluded-line muted">{{ note }}</p>
              }
              @if (forecastSummary(); as f) {
                <p class="forecast-line">
                  <span class="forecast-key" aria-hidden="true"></span>
                  <span>Next 7 days: about <strong>{{ plat(f.predicted) }}</strong> on average, likely {{ plat(f.low) }}–{{ plat(f.high) }}</span>
                  @if (f.method === 'baseline') {
                    <span class="muted">· Baseline forecast: the model isn't reliable for items like this yet
                      (<a routerLink="/forecast">why</a>).</span>
                  } @else {
                    <span class="muted">· Model forecast (<a routerLink="/forecast">how it was tested</a>).</span>
                  }
                </p>
              }
            } @else {
              <app-empty-state title="No price history recorded">
                Backfill this item with <code>python -m wfm backfill {{ it.slug }}</code>.
              </app-empty-state>
            }
          </section>
          <section class="card">
            <div class="card-header">
              <div><h2>Trade volume</h2><p>Completed trades per {{ granularity() === 'day' ? 'day' : 'hour' }}.</p></div>
            </div>
            @if (volumePoints().length) {
              <app-column-chart [points]="volumePoints()" [granularity]="granularity()" [height]="260" label="Trades" ariaLabel="Trade volume" />
            } @else {
              <app-empty-state title="No volume recorded">Volume comes from the same backfill as price history.</app-empty-state>
            }
          </section>
        </div>

        <section class="card">
          <div class="card-header">
            <div><h2>Best current orders</h2><p>Top 5 each way from players online now. Fetched live, not stored.</p></div>
            <span class="top-actions">
              @if (top.value(); as t) { <span class="muted">Fetched {{ ago(t.fetchedAt) }}</span> }
              <button class="btn small" type="button" (click)="top.reload()" [disabled]="top.isLoading()">
                {{ top.isLoading() ? 'Loading…' : 'Refresh' }}</button>
            </span>
          </div>
          @if (top.error()) {
            <app-empty-state title="warframe.market did not respond">The API is slow or rate limited. Try again in a minute.</app-empty-state>
          } @else if (top.value(); as t) {
            <div class="grid grid-2">
              <div>
                <h3 class="side">Sellers <span class="muted">· lowest first</span></h3>
                <ng-container *ngTemplateOutlet="orders; context: { $implicit: t.sell }" />
                @if (!t.sell.length) { <p class="muted">No online sellers.</p> }
              </div>
              <div>
                <h3 class="side">Buyers <span class="muted">· highest first</span></h3>
                <ng-container *ngTemplateOutlet="orders; context: { $implicit: t.buy }" />
                @if (!t.buy.length) { <p class="muted">No online buyers.</p> }
              </div>
            </div>
          } @else {
            <p class="muted">Loading current orders from warframe.market…</p>
          }
        </section>

        @if (it.setItems.length) {
          <section class="card">
            <div class="card-header"><div><h2>Set contents</h2><p>Every part of this set, with ducat values.</p></div></div>
            <table class="data">
              <thead><tr><th>Part</th><th class="r">Qty in set</th><th class="r">Ducats</th><th class="r">Latest price</th></tr></thead>
              <tbody>
                @for (part of it.setItems; track part.id) {
                  <tr>
                    <td><a class="item-link" [routerLink]="['/items', part.slug]">
                      <app-item-thumb [image]="part.image" [name]="part.name" [size]="24" />{{ part.name }}</a></td>
                    <td class="r">{{ num(part.quantity_in_set) }}</td>
                    <td class="r">{{ num(part.ducats) }}</td>
                    <td class="r">{{ plat(part.price) }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </section>
        }
      }
    </div>

    <ng-template #orders let-list>
      @if (list.length) {
        <table class="data">
          <thead><tr><th>Player</th><th class="r">Rep</th><th class="r">Qty</th><th class="r">Price</th></tr></thead>
          <tbody>
            @for (o of list; track o.id) {
              <tr><td>{{ o.user.ingameName }} <span class="muted">{{ status(o) }}</span></td>
                <td class="r">{{ o.user.reputation }}</td><td class="r">{{ o.quantity }}</td>
                <td class="r strong">{{ plat(o.platinum) }}</td></tr>
            }
          </tbody>
        </table>
      }
    </ng-template>
  `,
  styles: `
    .back { display: inline-flex; align-items: center; gap: 6px; align-self: flex-start; text-decoration: none; font-size: 13px; }
    .back:hover { text-decoration: underline; }
    .hero { display: flex; gap: 18px; align-items: flex-start; }
    .hero-text { flex: 1; min-width: 0; }
    .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
    .description { margin: 10px 0 0; color: var(--text-secondary); white-space: pre-line; max-width: 720px; }
    .hero-actions { display: flex; gap: 8px; }
    @media (max-width: 600px) {
      .hero { flex-wrap: wrap; }
      .hero-actions { flex-basis: 100%; }
    }
    .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 14px; }
    .fact-label { color: var(--text-muted); font-size: 12px; }
    .fact-value { font-size: 16px; font-weight: 600; }
    .charts { grid-template-columns: 3fr 2fr; }
    @media (max-width: 1100px) { .charts { grid-template-columns: 1fr; } }
    .side { font-size: 13px; margin-bottom: 6px; }
    .strong { font-weight: 600; }
    .top-actions { display: inline-flex; align-items: center; gap: 10px; }
    .refetching { opacity: 0.6; transition: opacity 0.15s; }
    .excluded-line { margin: 10px 0 0; font-size: 12.5px; }
    .forecast-line { margin: 10px 0 0; font-size: 13px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .forecast-key { width: 14px; border-top: 2px dashed var(--series-1); }
  `,
})
export class ItemDetailPage {
  private readonly api = inject(ApiService);

  /** Bound from the :slug route parameter (withComponentInputBinding). */
  readonly slug = input.required<string>();

  protected readonly granularity = signal<Granularity>('day');
  protected readonly item = rxResource({ params: () => this.slug(), stream: ({ params }) => this.api.item(params) });
  /** Selected rank; resets to the backend's default rank whenever the item changes. */
  protected readonly rank = linkedSignal<number | null>(() => this.item.value()?.default_rank ?? null);

  protected readonly history = rxResource({
    params: () => ({ slug: this.slug(), granularity: this.granularity(), rank: this.rank() }),
    stream: ({ params }) => this.api.history(params.slug, params.granularity, params.rank),
  });
  protected readonly top = rxResource({
    params: () => ({ slug: this.slug(), rank: this.rank() }),
    stream: ({ params }) => this.api.top(params.slug, params.rank),
  });

  protected readonly forecast = rxResource({
    params: () => ({ slug: this.slug(), rank: this.rank() }),
    stream: ({ params }) => this.api.forecast(params.slug, params.rank),
  });

  private readonly lastHistory = keepLast(this.history.value);

  /** The 7-day forecast (the average price over the next 7 days), shown on the daily chart only. */
  protected readonly forecastSummary = computed(() =>
    this.granularity() === 'day' ? (this.forecast.value() ?? []).find((f) => f.horizon === 7) ?? null : null,
  );

  protected readonly forecastNote = computed(() => (this.forecastSummary() ? ', with the forecast (dashed)' : ''));

  /** Tags minus the ones that only repeat the category (e.g. "mod" on a Mod). */
  protected readonly chipTags = computed(() => {
    const it = this.item.value();
    if (!it) return [];
    return it.tags.filter((tag) => tag.toLowerCase() !== it.category.toLowerCase());
  });

  protected readonly facts = computed(() => {
    const it = this.item.value();
    if (!it) return [];
    const facts = [
      { label: 'Latest median price', value: formatPlat(it.price) },
      { label: 'Ducats', value: formatNumber(it.ducats) },
      { label: 'Trading tax', value: formatCredits(it.trading_tax) },
      { label: 'Mastery rank', value: formatNumber(it.req_mastery_rank) },
      { label: 'Max rank', value: formatNumber(it.max_rank) },
      { label: 'Orders recorded', value: formatNumber(it.recordedOrders) },
    ];
    if (it.bulk_tradable !== null) facts.push({ label: 'Bulk tradable', value: it.bulk_tradable ? 'Yes' : 'No' });
    if (!it.detail_synced_at) facts.push({ label: 'Details', value: 'Not synced yet' });
    else facts.push({ label: 'Details synced', value: formatDateTime(it.detail_synced_at) });
    return facts;
  });

  protected readonly priceSeries = computed<Series[]>(() => {
    const h = this.lastHistory();
    if (!h) return [];
    const series: Series[] = [];
    if (h.closed.length) {
      series.push({
        key: 'closed', label: 'Median trade', color: 'var(--series-1)',
        points: h.closed.map((p) => ({ t: Date.parse(p.bucket), v: p.median })),
      });
    }
    // When every listing is a joke there is no listings line at all.
    if (h.live.sell.some((p) => p.ask !== null)) {
      series.push({
        key: 'listed', label: 'Sell listings', color: 'var(--series-2)',
        points: h.live.sell.map((p) => ({ t: Date.parse(p.bucket), v: p.ask })),
      });
    }
    const forecasts = this.granularity() === 'day' ? this.forecast.value() ?? [] : [];
    if (forecasts.length) {
      // Forecasts continue the trade-price line: same color, dashed, with their likely range.
      // Tomorrow's price is a point on day 1; a longer horizon is an average over days 1..h, so it
      // is drawn flat across those days.
      const t0 = Date.parse(forecasts[0].base_date);
      const at = (days: number) => t0 + days * 86_400_000;
      for (const f of forecasts) {
        const flat = f.horizon > 1;
        const from = flat ? { t: at(1), v: f.predicted, low: f.low, high: f.high }
                          : { t: t0, v: f.base_price, low: f.base_price, high: f.base_price };
        const to = { t: at(f.horizon), v: f.predicted, low: f.low, high: f.high };
        series.push({
          key: `forecast-${f.horizon}`, label: flat ? `Next ${f.horizon} days (avg)` : 'Tomorrow',
          color: 'var(--series-1)', dashed: true,
          points: [from, to].map(({ t, v }) => ({ t, v })),
          range: [from, to].map(({ t, low, high }) => ({ t, low, high })),
        });
      }
    }
    return series;
  });

  /** Names the days left out as likely faked trades, so nothing disappears silently. */
  protected readonly excludedNote = computed(() => {
    const excluded = this.lastHistory()?.excluded ?? [];
    if (!excluded.length) return null;
    // 99,999, not 100K: the oddness is the point
    const exact = (n: number | null) => (n ?? 0).toLocaleString('en-US');
    const when = (bucket: string) => (this.granularity() === 'day' ? formatDay(Date.parse(bucket)) : formatDateTime(bucket));
    const shown = excluded.slice(0, 3)
      .map((p) => `${when(p.bucket)} (${exact(p.volume)} at ${exact(p.median)}p)`).join(', ');
    const more = excluded.length > 3 ? ` and ${excluded.length - 3} more` : '';
    const what = excluded.length === 1 ? 'one entry' : `${excluded.length} entries`;
    return `Left out ${what} that look like faked trades: ${shown}${more}. They stay in the data but don't count toward prices or volume.`;
  });

  protected readonly volumePoints = computed(() =>
    (this.lastHistory()?.closed ?? []).map((p) => ({ t: Date.parse(p.bucket), v: p.volume })),
  );

  protected watch(): void {
    this.api.watch(this.slug(), null).subscribe(() => this.item.reload());
  }

  protected unwatch(): void {
    this.api.unwatch(this.slug()).subscribe(() => this.item.reload());
  }

  protected status(order: LiveOrder): string {
    return order.user.status === 'ingame' ? '· in game' : order.user.status === 'online' ? '· online' : '';
  }

  protected readonly num = formatNumber;
  protected readonly plat = formatPlat;
  protected readonly ago = timeAgo;
  protected readonly title = titleCase;
}
