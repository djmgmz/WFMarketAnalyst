import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'overview' },
  {
    path: 'overview',
    title: 'Overview · WF Market Analyst',
    loadComponent: () => import('./features/overview/overview.page').then((m) => m.OverviewPage),
  },
  {
    path: 'items',
    title: 'Items · WF Market Analyst',
    loadComponent: () => import('./features/items/items.page').then((m) => m.ItemsPage),
  },
  {
    path: 'items/:slug',
    title: 'Item · WF Market Analyst',
    loadComponent: () => import('./features/item-detail/item-detail.page').then((m) => m.ItemDetailPage),
  },
  {
    path: 'ducats',
    title: 'Ducats · WF Market Analyst',
    loadComponent: () => import('./features/ducats/ducats.page').then((m) => m.DucatsPage),
  },
  {
    path: 'forecast',
    title: 'Forecast · WF Market Analyst',
    loadComponent: () => import('./features/forecast/forecast.page').then((m) => m.ForecastPage),
  },
  {
    path: 'watchlist',
    title: 'Watchlist · WF Market Analyst',
    loadComponent: () => import('./features/watchlist/watchlist.page').then((m) => m.WatchlistPage),
  },
  { path: '**', redirectTo: 'overview' },
];
