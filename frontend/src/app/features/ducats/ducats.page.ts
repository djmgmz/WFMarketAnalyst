import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';

import { ApiService } from '../../core/api.service';
import { formatNumber, formatPlat } from '../../core/format';
import { RankedBarsComponent, RankedRow } from '../../shared/charts/ranked-bars.component';
import { EmptyStateComponent } from '../../shared/empty-state.component';
import { ItemThumbComponent } from '../../shared/item-thumb.component';
import { StatTileComponent } from '../../shared/stat-tile.component';

/** Prime parts ranked by ducats per platinum: what to sell to Baro Ki'Teer instead of players. */
@Component({
  selector: 'app-ducats-page',
  imports: [RouterLink, RankedBarsComponent, EmptyStateComponent, ItemThumbComponent, StatTileComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Ducats</h1>
          <p>Prime parts ranked by ducats per platinum. High ratios are worth more to Baro Ki'Teer than to players.</p>
        </div>
      </header>

      @if (rows(); as list) {
        <section class="kpi-row">
          <app-stat-tile label="Prime parts" [value]="num(list.length)" />
          <app-stat-tile label="Parts with a recorded price" [value]="num(priced().length)"
                         [note]="priced().length ? null : 'Backfill prices to rank by ratio'" />
          <app-stat-tile label="Best ducats per platinum" [value]="best()" />
        </section>

        <section class="card">
          <div class="card-header">
            <div><h2>Best value for ducats</h2><p>Top 12 parts by ducats per platinum.</p></div>
          </div>
          @if (topRows().length) {
            <app-ranked-bars [data]="topRows()" [format]="ratio" ariaLabel="Ducats per platinum" />
          } @else {
            <app-empty-state title="No prices recorded yet">
              Run <code>python -m wfm backfill --category Prime</code> to fetch price history for prime parts.
            </app-empty-state>
          }
        </section>

        <div class="filter-row">
          <input class="input" type="search" placeholder="Filter parts" aria-label="Filter parts"
                 (input)="filter.set($any($event.target).value)" />
        </div>

        <section class="card">
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>Part</th><th class="r">Ducats</th><th class="r">Latest price</th><th class="r">Ducats / plat</th></tr></thead>
              <tbody>
                @for (row of filtered(); track row.id) {
                  <tr>
                    <td><a class="item-link" [routerLink]="['/items', row.slug]">
                      <app-item-thumb [path]="row.thumb" [name]="row.name" [size]="24" />{{ row.name }}</a>
                      @if (row.vaulted) { <span class="chip">Vaulted</span> }</td>
                    <td class="r">{{ num(row.ducats) }}</td>
                    <td class="r">{{ plat(row.price) }}</td>
                    <td class="r">{{ row.ducats_per_plat ?? '–' }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        </section>
      } @else if (ducats.error()) {
        <app-empty-state title="Could not load ducat data">Is the backend running on port 8000?</app-empty-state>
      }
    </div>
  `,
  styles: `
    .item-link { display: inline-flex; align-items: center; gap: 8px; text-decoration: none; margin-right: 6px; }
    .item-link:hover { text-decoration: underline; }
  `,
})
export class DucatsPage {
  private readonly api = inject(ApiService);

  protected readonly ducats = rxResource({ stream: () => this.api.ducats() });
  protected readonly filter = signal('');

  protected readonly rows = computed(() => this.ducats.value());
  protected readonly priced = computed(() => (this.rows() ?? []).filter((r) => r.ducats_per_plat !== null));
  protected readonly filtered = computed(() => {
    const term = this.filter().trim().toLowerCase();
    const rows = this.rows() ?? [];
    return term ? rows.filter((r) => r.name.toLowerCase().includes(term)) : rows;
  });
  protected readonly topRows = computed<RankedRow[]>(() =>
    this.priced().slice(0, 12).map((r) => ({
      key: r.id, label: r.name.replace(' Prime', ''), value: r.ducats_per_plat as number, link: `/items/${r.slug}`,
    })),
  );
  protected readonly best = computed(() => {
    const top = this.priced()[0];
    return top ? `${top.ducats_per_plat}` : '–';
  });

  protected readonly ratio = (v: number) => `${v} ducats/p`;
  protected readonly num = formatNumber;
  protected readonly plat = formatPlat;
}
