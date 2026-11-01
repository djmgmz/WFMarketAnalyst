import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  CategoryCount, DucatRow, Forecast, ForecastReport, ForecastSearch, Granularity, History, ItemDetail, ItemPage, ItemQuery, Job,
  JobName, JobStartOptions, MoverRanking, Movers, PredictedMovers,
  Overview, RecentOrder, TagCount, TopOrders, WatchRow,
} from './models';

/** Typed access to the local FastAPI backend. Only `top()` reaches warframe.market live. */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  overview(): Observable<Overview> {
    return this.http.get<Overview>('/api/overview');
  }

  jobs(): Observable<Job[]> {
    return this.http.get<Job[]>('/api/jobs');
  }

  startJob(name: JobName, options: JobStartOptions = {}): Observable<unknown> {
    return this.http.post(`/api/jobs/${name}/start`, options);
  }

  stopJob(name: JobName): Observable<unknown> {
    return this.http.post(`/api/jobs/${name}/stop`, {});
  }

  categories(): Observable<CategoryCount[]> {
    return this.http.get<CategoryCount[]>('/api/categories');
  }

  tags(category?: string): Observable<TagCount[]> {
    return this.http.get<TagCount[]>('/api/tags', { params: toParams({ category }) });
  }

  items(query: ItemQuery): Observable<ItemPage> {
    return this.http.get<ItemPage>('/api/items', { params: toParams({ ...query }) });
  }

  item(slug: string): Observable<ItemDetail> {
    return this.http.get<ItemDetail>(`/api/items/${encodeURIComponent(slug)}`);
  }

  history(slug: string, granularity: Granularity, rank: number | null, subtype: string | null): Observable<History> {
    return this.http.get<History>(`/api/items/${encodeURIComponent(slug)}/history`, {
      params: toParams({ granularity, rank: rank ?? undefined, subtype: subtype ?? undefined }),
    });
  }

  top(slug: string, rank: number | null, subtype: string | null): Observable<TopOrders> {
    return this.http.get<TopOrders>(`/api/items/${encodeURIComponent(slug)}/top`, {
      params: toParams({ rank: rank ?? undefined, subtype: subtype ?? undefined }),
    });
  }

  recentOrders(limit = 25): Observable<RecentOrder[]> {
    return this.http.get<RecentOrder[]>('/api/orders/recent', { params: toParams({ limit }) });
  }

  forecast(slug: string, rank: number | null): Observable<Forecast[]> {
    return this.http.get<Forecast[]>(`/api/items/${encodeURIComponent(slug)}/forecast`, {
      params: toParams({ rank: rank ?? undefined }),
    });
  }

  /** Biggest predicted rises and falls, ranked by platinum gained (default) or percentage. */
  forecastMovers(horizon = 7, options: { limit?: number; by?: MoverRanking } = {}): Observable<PredictedMovers> {
    return this.http.get<PredictedMovers>('/api/forecast/movers', {
      params: toParams({ horizon, limit: options.limit, by: options.by }),
    });
  }

  forecastSearch(q: string, horizon = 7): Observable<ForecastSearch> {
    return this.http.get<ForecastSearch>('/api/forecast/search', { params: toParams({ q, horizon }) });
  }

  forecastReport(): Observable<ForecastReport | null> {
    return this.http.get<ForecastReport | null>('/api/forecast/report');
  }

  ducats(limit = 1000): Observable<DucatRow[]> {
    return this.http.get<DucatRow[]>('/api/ducats', { params: toParams({ limit }) });
  }

  movers(days = 7): Observable<Movers> {
    return this.http.get<Movers>('/api/movers', { params: toParams({ days }) });
  }

  watchlist(): Observable<WatchRow[]> {
    return this.http.get<WatchRow[]>('/api/watchlist');
  }

  watch(slug: string, targetPrice: number | null): Observable<unknown> {
    return this.http.put(`/api/watchlist/${encodeURIComponent(slug)}`, { target_price: targetPrice });
  }

  unwatch(slug: string): Observable<unknown> {
    return this.http.delete(`/api/watchlist/${encodeURIComponent(slug)}`);
  }
}

function toParams(values: Record<string, string | number | boolean | undefined | null>): HttpParams {
  let params = new HttpParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') {
      params = params.set(key, String(value));
    }
  }
  return params;
}
