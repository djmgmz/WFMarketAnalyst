import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { rxResource, toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { map } from 'rxjs';

import { ApiService } from '../../core/api.service';
import { formatCredits, formatNumber, formatPlat, titleCase } from '../../core/format';
import { keepLast } from '../../core/keep-last';
import { ItemQuery } from '../../core/models';
import { EmptyStateComponent } from '../../shared/empty-state.component';
import { IconComponent } from '../../shared/icon.component';
import { ItemThumbComponent } from '../../shared/item-thumb.component';

const PAGE_SIZE = 50;
const SORTS: { value: NonNullable<ItemQuery['sort']>; label: string }[] = [
  { value: 'name', label: 'Name' },
  { value: 'price', label: 'Latest price' },
  { value: 'ducats', label: 'Ducats' },
  { value: 'mastery', label: 'Mastery rank' },
  { value: 'tax', label: 'Trading tax' },
];

/** Searchable catalog. The URL query string is the single source of truth for filters. */
@Component({
  selector: 'app-items-page',
  imports: [RouterLink, ItemThumbComponent, EmptyStateComponent, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="page">
      <header class="page-header">
        <div>
          <h1>Items</h1>
          <p>{{ total() }} tradable items{{ query().category ? ' in ' + query().category : '' }}.</p>
        </div>
      </header>

      <div class="filter-row" role="search">
        <input class="input" type="search" placeholder="Search items" aria-label="Search items"
               [value]="query().q ?? ''" (input)="onSearch($any($event.target).value)" />
        <select class="select" aria-label="Category"
                (change)="update({ category: $any($event.target).value || null, tag: null })">
          <option value="">All categories</option>
          @for (c of categories.value() ?? []; track c.category) {
            @if (c.count) { <option [value]="c.category" [selected]="c.category === query().category">{{ c.category }} ({{ c.count }})</option> }
          }
        </select>
        <select class="select" aria-label="Tag"
                (change)="update({ tag: $any($event.target).value || null })">
          <option value="">Any tag</option>
          @for (t of tags.value() ?? []; track t.tag) {
            <option [value]="t.tag" [selected]="t.tag === query().tag">{{ title(t.tag) }} ({{ t.count }})</option>
          }
        </select>
        <select class="select" aria-label="Vaulted" [value]="vaultedValue()"
                (change)="update({ vaulted: $any($event.target).value || null })">
          <option value="">Vaulted or not</option>
          <option value="true">Vaulted only</option>
          <option value="false">Not vaulted</option>
        </select>
        <select class="select" aria-label="Sort by" [value]="query().sort ?? 'name'"
                (change)="update({ sort: $any($event.target).value })">
          @for (s of sorts; track s.value) { <option [value]="s.value">Sort: {{ s.label }}</option> }
        </select>
        <button class="btn ghost" type="button" (click)="toggleOrder()"
                [attr.aria-label]="query().order === 'desc' ? 'Descending' : 'Ascending'">
          {{ query().order === 'desc' ? '↓ Desc' : '↑ Asc' }}</button>
        @if (hasFilters()) {
          <button class="btn ghost" type="button" (click)="clear()">Clear filters</button>
        }
      </div>

      <section class="card" [class.refetching]="page.isLoading()">
        @if (rows().length) {
          <div class="table-wrap">
            <table class="data">
              <thead>
                <tr><th>Item</th><th>Category</th><th>Tags</th><th class="r">Latest price</th>
                  <th class="r">Ducats</th><th class="r">MR</th><th class="r">Trading tax</th></tr>
              </thead>
              <tbody>
                @for (item of rows(); track item.id) {
                  <tr>
                    <td>
                      <a class="item-link" [routerLink]="['/items', item.slug]">
                        <app-item-thumb [image]="item.image" [name]="item.name" />
                        <span>{{ item.name }}</span>
                        @if (item.watched) { <span class="watched" title="On watchlist" role="img" aria-label="On watchlist"><app-icon class="filled" name="watchlist" [size]="12" /></span> }
                      </a>
                    </td>
                    <td class="secondary">{{ item.category }}</td>
                    <td><div class="tags">
                      @for (tag of item.tags.slice(0, 3); track tag) { <span class="chip">{{ title(tag) }}</span> }
                      @if (item.vaulted) { <span class="chip">Vaulted</span> }
                    </div></td>
                    <td class="r">{{ plat(item.price) }}</td>
                    <td class="r">{{ num(item.ducats) }}</td>
                    <td class="r">{{ num(item.req_mastery_rank) }}</td>
                    <td class="r">{{ credits(item.trading_tax) }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
          <footer class="pager">
            <span class="muted">{{ rangeLabel() }}</span>
            <span class="pager-buttons">
              <button class="btn small" type="button" [disabled]="offset() === 0" (click)="goTo(offset() - pageSize)">Previous</button>
              <button class="btn small" type="button" [disabled]="offset() + pageSize >= total()" (click)="goTo(offset() + pageSize)">Next</button>
            </span>
          </footer>
        } @else if (page.error()) {
          <app-empty-state title="Could not load items">Is the backend running on port 8000?</app-empty-state>
        } @else if (!page.isLoading()) {
          <app-empty-state title="No items match">Try a different search or clear the filters.</app-empty-state>
        }
      </section>
    </div>
  `,
  styles: `
    .item-link { gap: 10px; font-weight: 500; }
    .item-link:hover { text-decoration: none; }
    .item-link:hover span:first-of-type { text-decoration: underline; }
    .watched { display: inline-flex; vertical-align: -1px; margin-left: 6px; color: var(--text-muted); }
    .tags { display: flex; gap: 4px; flex-wrap: wrap; }
    .pager { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; }
    .pager-buttons { display: inline-flex; gap: 6px; }
    .refetching { opacity: 0.6; transition: opacity 0.15s; }
  `,
})
export class ItemsPage {
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private searchTimer: ReturnType<typeof setTimeout> | undefined;

  protected readonly sorts = SORTS;
  protected readonly pageSize = PAGE_SIZE;

  protected readonly query = toSignal(
    this.route.queryParamMap.pipe(
      map((p): ItemQuery => ({
        q: p.get('q') ?? undefined,
        category: p.get('category') ?? undefined,
        tag: p.get('tag') ?? undefined,
        vaulted: p.has('vaulted') ? p.get('vaulted') === 'true' : undefined,
        sort: (p.get('sort') as ItemQuery['sort']) ?? 'name',
        order: p.get('order') === 'desc' ? 'desc' : 'asc',
        offset: Number(p.get('offset') ?? 0) || 0,
        limit: PAGE_SIZE,
      })),
    ),
    { requireSync: true },
  );

  protected readonly categories = rxResource({ stream: () => this.api.categories() });
  protected readonly tags = rxResource({
    params: () => this.query().category,
    stream: ({ params }) => this.api.tags(params),
  });
  protected readonly page = rxResource({
    params: () => this.query(),
    stream: ({ params }) => this.api.items(params),
  });

  private readonly lastPage = keepLast(this.page.value);
  protected readonly rows = computed(() => this.lastPage()?.items ?? []);
  protected readonly total = computed(() => this.lastPage()?.total ?? 0);
  protected readonly offset = computed(() => this.query().offset ?? 0);
  protected readonly vaultedValue = computed(() => {
    const v = this.query().vaulted;
    return v === undefined ? '' : String(v);
  });
  protected readonly hasFilters = computed(() => {
    const q = this.query();
    return !!(q.q || q.category || q.tag || q.vaulted !== undefined);
  });
  protected readonly rangeLabel = computed(() => {
    const total = this.total();
    const start = Math.min(total, this.offset() + 1);
    const end = Math.min(total, this.offset() + PAGE_SIZE);
    return `${formatNumber(start)}–${formatNumber(end)} of ${formatNumber(total)}`;
  });

  protected update(changes: Record<string, string | number | null>): void {
    // Any filter change returns to the first page.
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { offset: null, ...changes },
      queryParamsHandling: 'merge',
    });
  }

  protected onSearch(value: string): void {
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => this.update({ q: value.trim() || null }), 250);
  }

  protected toggleOrder(): void {
    this.update({ order: this.query().order === 'desc' ? 'asc' : 'desc' });
  }

  protected goTo(offset: number): void {
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { offset: offset || null },
      queryParamsHandling: 'merge',
    });
  }

  protected clear(): void {
    this.router.navigate([], { relativeTo: this.route, queryParams: {} });
  }

  protected readonly num = formatNumber;
  protected readonly plat = formatPlat;
  protected readonly credits = formatCredits;
  protected readonly title = titleCase;
}
