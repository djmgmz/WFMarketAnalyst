import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';

import { ApiService } from '../../core/api.service';
import { formatNumber, formatPercent, formatPlat, horizonLabel, timeAgo } from '../../core/format';
import { JobsService } from '../../core/jobs.service';
import { ForecastMetrics, ItemForecast, MoverRanking } from '../../core/models';
import { RankedBarsComponent, RankedRow } from '../../shared/charts/ranked-bars.component';
import { EmptyStateComponent } from '../../shared/empty-state.component';
import { IconComponent } from '../../shared/icon.component';
import { ItemThumbComponent } from '../../shared/item-thumb.component';
import { StatTileComponent } from '../../shared/stat-tile.component';

const RANKINGS: { value: MoverRanking; label: string; note: string }[] = [
  { value: 'gain', label: 'Platinum', note: 'most platinum gained per item bought now' },
  { value: 'change', label: 'Percent', note: 'largest percentage rise' },
];
const MIN_SEARCH_LENGTH = 2;

/** Plain-language names for the model's inputs. */
const FEATURE_LABELS: Record<string, string> = {
  ret_1: 'Change over 1 day', ret_3: 'Change over 3 days', ret_7: 'Change over 7 days', ret_14: 'Change over 14 days',
  mean_gap_7: 'Price vs 7-day average', mean_gap_14: 'Price vs 14-day average', mean_gap_28: 'Price vs 28-day average',
  vol_7: '7-day volatility', vol_14: '14-day volatility', vol_28: '28-day volatility',
  log_volume: 'Trades that day', volume_7: 'Trades, 7-day average', volume_28: 'Trades, 28-day average',
  range_pos: 'Position in recent price range', ma_gap: 'Price vs moving average',
  listing_premium: 'Sell listings vs trade price', spread: 'Gap between buy and sell listings',
  log_supply: 'Number of sell listings', level: 'Price level', days_since_trade: 'Days since last trade',
  dow: 'Day of week', rank: 'Rank', max_rank: 'Max rank', is_max_rank: 'Is max rank', ducats: 'Ducats',
  vaulted: 'Vaulted', req_mastery_rank: 'Mastery rank', category: 'Category', rarity: 'Rarity', band: 'Price band',
};

const SEGMENT_LABELS: Record<string, string> = { category: 'Category', band: 'Price band' };

@Component({
  selector: 'app-forecast-page',
  imports: [NgTemplateOutlet, RouterLink, StatTileComponent, RankedBarsComponent, EmptyStateComponent, ItemThumbComponent,
            IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Forecast</h1>
          <p>Each item's price tomorrow and its average price over the next 7 days, with a likely range,
            tested against simply assuming no change.</p>
        </div>
        @if (report(); as r) {
          <div class="segmented" role="group" aria-label="Forecast horizon">
            @for (h of horizons(); track h) {
              <button type="button" [attr.aria-pressed]="horizon() === h" (click)="horizon.set(h)">{{ label(h) }}</button>
            }
          </div>
        }
      </header>

      @if (report(); as r) {
        @if (selected(); as hr) {
          <section class="card verdict" [class.good]="verdict().good">
            <div class="verdict-title">
              <app-icon [name]="verdict().good ? 'check' : 'warning'" [size]="18" />{{ verdict().title }}
            </div>
            <p>{{ verdict().body }}</p>
            <p class="muted">Trained {{ ago(r.made_at) }} on {{ num(r.series) }} price series (latest day {{ r.last_date }}).
              Tested on the {{ r.test_days }} days after {{ r.cutoff }}: {{ num(hr.train_rows) }} training rows,
              {{ num(hr.test_rows) }} test rows. {{ num(r.forecasts) }} forecasts stored.</p>
          </section>

          <section class="kpi-row" aria-label="Accuracy on the test days">
            <app-stat-tile label="Skill vs no change" [value]="pct(hr.overall.skill)"
                           note="Above 0% means smaller errors than assuming no change" />
            <app-stat-tile label="Direction right" [value]="share(hr.overall.direction)"
                           note="Of test days where the price moved" />
            <app-stat-tile label="Range coverage" [value]="share(hr.overall.coverage)"
                           note="How often the real price fell inside the range (target 80%)" />
            <app-stat-tile label="Typical error" [value]="plat(hr.overall.model_mae_plat)"
                           [note]="'No change: ' + plat(hr.overall.naive_mae_plat)" />
            <app-stat-tile label="Trend guess skill" [value]="pct(hr.overall.trend_skill)"
                           note="Assuming last week's trend continues" />
          </section>

          <div class="grid">
            <section class="card">
              <div class="card-header">
                <div><h2>Expected to rise</h2>
                  <p>The 10 largest predicted rises for {{ over() }}: {{ rankingNote() }}. Model forecasts only;
                    rises under 1p are left out, since prices are whole platinum.</p></div>
                <div class="segmented" role="group" aria-label="Rank rises by">
                  @for (r of rankings; track r.value) {
                    <button type="button" [attr.aria-pressed]="ranking() === r.value" (click)="ranking.set(r.value)">{{ r.label }}</button>
                  }
                </div>
              </div>
              @if (risingRows().length) {
                <ng-container *ngTemplateOutlet="forecastTable; context: { $implicit: risingRows(), method: false }" />
              } @else if (!rising.isLoading()) {
                <app-empty-state title="No rises predicted">
                  The model predicts no rise of 1p or more for {{ over() }}, or it isn't used for any group of items yet.
                </app-empty-state>
              }
            </section>

            <section class="card">
              <div class="card-header">
                <div><h2>Find an item</h2><p>Every stored forecast for {{ over() }}, model or baseline.</p></div>
              </div>
              <div class="filter-row" role="search">
                <input class="input" type="search" placeholder="Search items with a forecast" aria-label="Search items with a forecast"
                       [value]="searchInput()" (input)="onSearch($any($event.target).value)" />
              </div>
              @if (found(); as f) {
                @if (f.forecasts.length) {
                  <ng-container *ngTemplateOutlet="forecastTable; context: { $implicit: f.forecasts, method: true }" />
                  @if (f.total > f.forecasts.length) {
                    <p class="muted more">Showing {{ f.forecasts.length }} of {{ num(f.total) }} matches. Type more of the name to narrow it down.</p>
                  }
                } @else {
                  <app-empty-state title="No forecast for that name">
                    Only items with price history have forecasts. Backfill the item, then retrain.
                  </app-empty-state>
                }
              } @else {
                <p class="muted">Type at least 2 letters of an item's name.</p>
              }
            </section>
          </div>

          <section class="card">
            <div class="card-header">
              <div><h2>Where the model is used</h2>
                <p>An item gets the model's forecast only if it beats "no change" for both its category and its price band
                  (on at least 30 test rows). Everywhere else the forecast is "no change" with a range from past moves.</p></div>
            </div>
            <div class="table-wrap">
              <table class="data">
                <thead><tr><th>Group</th><th>Segment</th><th class="r">Test rows</th><th class="r">Skill vs no change</th>
                  <th class="r">Trend guess</th><th class="r">Range coverage</th><th>Forecast uses</th></tr></thead>
                <tbody>
                  @for (row of segmentRows(); track row.key) {
                    <tr>
                      <td class="secondary">{{ row.group }}</td>
                      <td>{{ row.name }}</td>
                      <td class="r">{{ num(row.m.n) }}</td>
                      <td class="r" [class.delta-up]="(row.m.skill ?? 0) > 0" [class.delta-down]="(row.m.skill ?? 0) < 0">{{ pct(row.m.skill) }}</td>
                      <td class="r">{{ pct(row.m.trend_skill) }}</td>
                      <td class="r">{{ share(row.m.coverage) }}</td>
                      <td>@if (row.m.uses_model) { <span class="chip model">✓ Model</span> } @else { <span class="chip">Baseline</span> }</td>
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          </section>
        }

        <div class="grid grid-2">
          <section class="card">
            <div class="card-header"><div><h2>What the model relies on</h2>
              <p>Share of the 7-day model's decisions driven by each input.</p></div></div>
            @if (featureRows().length) {
              <app-ranked-bars [data]="featureRows()" [format]="shareFmt" [labelWidth]="210" ariaLabel="Feature importance" />
            } @else {
              <app-empty-state title="No model trained">There was not enough data to train the model.</app-empty-state>
            }
          </section>
          <section class="card prose">
            <div class="card-header"><div><h2>How it was tested</h2></div></div>
            <ul>
              <li><strong>What it predicts.</strong> Tomorrow's price, and the average price over the next 7 days.
                A single day's median comes from a handful of trades and is mostly noise; the week's average is what an
                item can be expected to sell for.</li>
              <li><strong>One model for all items.</strong> It predicts the percentage change, not the price, so a 5p mod and a
                3,000p item are comparable. Item traits (category, rarity, rank, price level, volatility, volume) are inputs.</li>
              <li><strong>Walk-forward test.</strong> The last {{ r.test_days }} days were hidden during training, and nothing it
                learned from could see past {{ r.cutoff }}, the same way a real forecast can't see the future.</li>
              <li><strong>Judged against simple guesses.</strong> "No change" and "last week's trend continues". Errors are
                measured as percentages, so cheap and expensive items count equally.</li>
              <li><strong>Range.</strong> Two extra models estimate the 10th and 90th percentile, giving an 80% range.</li>
            </ul>
            <h3>Limitations</h3>
            <ul class="muted">
              <li>About three months of history; it should improve as the daily refresh adds more.</li>
              <li>Game updates and events (like Prime Resurgence rotations) can't be predicted from prices.</li>
              <li>Completed trades are self-reported on warframe.market, so they undercount real trading.</li>
              <li>A portfolio demonstration, not trading advice.</li>
            </ul>
          </section>
        </div>
      } @else if (!reportRes.isLoading()) {
        <app-empty-state title="The forecast hasn't been trained yet">
          Backfill some price history first (Overview → Background jobs → Price history backfill), then start
          <strong>Price forecast</strong> there, or run <code>python -m wfm forecast</code>.
        </app-empty-state>
      }
    </div>

    <ng-template #forecastTable let-rows let-method="method">
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Item</th><th class="r">Now</th><th class="r">{{ label(horizon()) }}</th><th class="r">Likely range</th>
            <th class="r">Gain</th><th class="r">Change</th>@if (method) { <th>Forecast</th> }</tr></thead>
          <tbody>
            @for (f of asRows(rows); track f.item.slug + ':' + f.rank) {
              <tr>
                <td><a class="item-link" [routerLink]="['/items', f.item.slug]">
                  <app-item-thumb [path]="f.item.thumb" [name]="f.item.name" [size]="22" />{{ f.item.name }}</a>
                  @if (f.rank !== null) { <span class="muted"> · rank {{ f.rank }}</span> }</td>
                <td class="r">{{ plat(f.base_price) }}</td>
                <td class="r">{{ plat(f.predicted) }}</td>
                <td class="r muted nowrap">{{ plat(f.low) }}–{{ plat(f.high) }}</td>
                <td class="r nowrap">{{ gainText(f.gain) }}</td>
                <td class="r nowrap" [class.delta-up]="f.change > 0" [class.delta-down]="f.change < 0">
                  @if (f.change === 0) { <span class="muted">no change</span> }
                  @else { {{ f.change > 0 ? '▲' : '▼' }} {{ pct(f.change) }} }</td>
                @if (method) {
                  <td>@if (f.method === 'model') { <span class="chip model">Model</span> } @else { <span class="chip">Baseline</span> }</td>
                }
              </tr>
            }
          </tbody>
        </table>
      </div>
    </ng-template>
  `,
  styles: `
    .verdict-title { display: flex; align-items: center; gap: 8px; font-family: var(--font-display); font-size: 18px;
                     font-weight: 600; letter-spacing: 0.01em; }
    .verdict-title app-icon { color: var(--status-warning); }
    .verdict.good { border-color: rgba(76, 195, 138, 0.35); }
    .verdict.good .verdict-title app-icon { color: var(--status-good); }
    .verdict p { margin: 6px 0 0; }
    .chip.model { color: var(--delta-up); border-color: currentColor; }
    .prose ul { margin: 0; padding-left: 18px; display: grid; gap: 6px; }
    .prose h3 { font-size: 13px; margin: 14px 0 6px; }
    .filter-row { margin-bottom: 12px; }
    .filter-row .input { flex: 1; }
    .more { margin: 10px 0 0; font-size: 13px; }
    .nowrap, .segmented button { white-space: nowrap; }
  `,
})
export class ForecastPage {
  private readonly api = inject(ApiService);
  private readonly jobs = inject(JobsService);

  protected readonly reportRes = rxResource({ stream: () => this.api.forecastReport() });
  protected readonly report = computed(() => this.reportRes.value() ?? null);
  protected readonly horizon = signal('7');
  protected readonly horizons = computed(() => Object.keys(this.report()?.horizons ?? {}));
  protected readonly selected = computed(() => this.report()?.horizons[this.horizon()] ?? null);
  /** What the selected horizon covers, for sentences: "tomorrow" / "the next 7 days (average price)". */
  protected readonly over = computed(() =>
    this.horizon() === '1' ? 'tomorrow' : `the next ${this.horizon()} days (average price)`);
  protected readonly label = horizonLabel;
  protected readonly rankings = RANKINGS;
  protected readonly ranking = signal<MoverRanking>('gain');
  protected readonly rankingNote = computed(() => RANKINGS.find((r) => r.value === this.ranking())!.note);

  protected readonly rising = rxResource({
    params: () => ({ horizon: Number(this.horizon()), by: this.ranking() }),
    stream: ({ params }) => this.api.forecastMovers(params.horizon, { limit: 10, by: params.by }),
  });
  protected readonly risingRows = computed(() => this.rising.value()?.gainers ?? []);

  /** What is typed in the search box; `search` follows it after a short pause. */
  protected readonly searchInput = signal('');
  private readonly search = signal('');
  private searchTimer: ReturnType<typeof setTimeout> | undefined;
  protected readonly searchRes = rxResource({
    params: () => (this.search().length >= MIN_SEARCH_LENGTH ? { q: this.search(), horizon: Number(this.horizon()) } : undefined),
    stream: ({ params }) => this.api.forecastSearch(params.q, params.horizon),
  });
  /** Null until enough is typed, so the hint shows instead of stale results. */
  protected readonly found = computed(() => (this.search().length >= MIN_SEARCH_LENGTH ? this.searchRes.value() ?? null : null));

  constructor() {
    // A training run finished: show its report and the new forecasts.
    let first = true;
    effect(() => {
      this.jobs.progressKey();
      if (first) {
        first = false;
        return;
      }
      untracked(() => {
        this.reportRes.reload();
        this.rising.reload();
        this.searchRes.reload();
      });
    });
  }

  protected onSearch(value: string): void {
    this.searchInput.set(value);
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => this.search.set(value.trim()), 250);
  }

  /** Types the rows handed to the shared table template (template context is untyped). */
  protected readonly asRows = (rows: ItemForecast[]) => rows;
  /** +2.5p / -0.4p; a dash for moves that round to nothing. */
  protected readonly gainText = (gain: number) =>
    Math.abs(gain) < 0.05 ? '–' : `${gain > 0 ? '+' : ''}${formatPlat(gain)}`;

  protected readonly verdict = computed(() => {
    const hr = this.selected();
    const days = this.horizon() === '1' ? "Tomorrow's" : `The ${this.horizon()}-day`;
    if (!hr || hr.status !== 'tested') {
      return { good: false, title: 'Not enough history to test the model yet',
               body: 'Every forecast is "no change" with a range from past moves. Backfill more items and retrain.' };
    }
    const skill = hr.overall.skill ?? 0;
    return skill > 0
      ? { good: true, title: `${days} forecast beats "no change" by ${formatPercent(skill)}`,
          body: 'Its errors are smaller than assuming prices stay where they are. It is still only used for the segments below where it wins.' }
      : { good: false, title: `${days} forecast doesn't beat "no change" yet (${formatPercent(skill)})`,
          body: 'Forecasts fall back to "no change" with a range from past moves wherever the model loses. More items and longer history should help.' };
  });

  protected readonly segmentRows = computed(() => {
    const hr = this.selected();
    if (!hr) return [];
    return Object.entries(hr.segments).flatMap(([group, values]) =>
      Object.entries(values)
        .sort((a, b) => b[1].n - a[1].n)
        .map(([name, m]) => ({ key: `${group}:${name}`, group: SEGMENT_LABELS[group] ?? group, name, m: m as ForecastMetrics })),
    );
  });

  protected readonly featureRows = computed<RankedRow[]>(() =>
    (this.report()?.feature_importance ?? []).slice(0, 10)
      .map((f) => ({ key: f.feature, label: FEATURE_LABELS[f.feature] ?? f.feature, value: f.share })),
  );

  protected readonly pct = (v: number | null | undefined) => (v === null || v === undefined ? '–' : formatPercent(v));
  protected readonly share = (v: number | null | undefined) => (v === null || v === undefined ? '–' : `${Math.round(v * 100)}%`);
  protected readonly shareFmt = (v: number) => `${(v * 100).toFixed(1)}%`;
  protected readonly plat = formatPlat;
  protected readonly num = formatNumber;
  protected readonly ago = timeAgo;
}
