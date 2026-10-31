// Shapes returned by the FastAPI backend (backend/wfm/api.py).

export type Category =
  | 'Relic' | 'Arcane' | 'Mod' | 'Prime' | 'Ayatan' | 'Cosmetic' | 'Resource' | 'Gear' | 'Other';

export interface ItemSummary {
  id: string;
  slug: string;
  name: string;
  category: Category;
  tags: string[];
  rarity: string | null;
  max_rank: number | null;
  ducats: number | null;
  req_mastery_rank: number | null;
  trading_tax: number | null;
  vaulted: boolean | null;
  /** File name under /api/images/, set by the item-images job; null when there is no image. */
  image: string | null;
  price: number | null;
  watched?: boolean;
}

export interface SetItem extends ItemSummary {
  set_root: boolean | null;
  quantity_in_set: number | null;
}

export interface ItemDetail extends ItemSummary {
  game_ref: string | null;
  description: string | null;
  wiki_link: string | null;
  sub_icon: string | null;
  max_charges: number | null;
  vosfor: number | null;
  tradable: boolean | null;
  bulk_tradable: boolean | null;
  subtypes: string[] | null;
  set_root: boolean | null;
  set_parts: string[] | null;
  detail_synced_at: string | null;
  setItems: SetItem[];
  watch: { target_price: number | null; added_at: string } | null;
  /** Rank prices refer to by default: 0 for ranked items, null for items without ranks. */
  default_rank: number | null;
  /** Ranks with recorded price history (null = the item has no ranks). */
  historyRanks: (number | null)[];
  recordedOrders: number;
}

export interface ItemPage {
  total: number;
  items: ItemSummary[];
}

export interface CategoryCount {
  category: Category;
  count: number;
}

export interface TagCount {
  tag: string;
  count: number;
}

export interface Overview {
  catalog: {
    items: number;
    detailsSynced: number;
    byCategory: CategoryCount[];
    primeParts: number;
    relicsVaulted: number;
    relicsTotal: number;
    auxiliary: Record<string, number>;
    listSyncedAt: string | null;
    detailSyncedAt: string | null;
  };
  recording: {
    ordersTotal: number;
    orders24h: number;
    lastOrderAt: string | null;
    itemsWithHistory: number;
    statsBackfilledAt: string | null;
    snapshots: number;
    online: { connections: number; authorizedUsers: number } | null;
  };
  watchlist: number;
}

export interface ClosedPoint {
  bucket: string;
  volume: number | null;
  min_price: number | null;
  max_price: number | null;
  open_price: number | null;
  closed_price: number | null;
  avg_price: number | null;
  wa_price: number | null;
  median: number | null;
  moving_avg: number | null;
}

export interface LivePoint {
  bucket: string;
  volume: number | null;
  min_price: number | null;
  max_price: number | null;
  median: number | null;
}

export type Granularity = 'day' | 'hour';

export interface History {
  /** null for items without ranks. */
  rank: number | null;
  granularity: Granularity;
  closed: ClosedPoint[];
  live: { sell: LivePoint[]; buy: LivePoint[] };
}

export interface OrderUser {
  id: string;
  ingameName: string;
  slug: string;
  reputation: number;
  platform: string;
  crossplay: boolean;
  status: 'ingame' | 'online' | 'offline';
  lastSeen?: string;
}

export interface LiveOrder {
  id: string;
  type: 'buy' | 'sell';
  platinum: number;
  quantity: number;
  perTrade?: number;
  rank?: number;
  createdAt: string;
  updatedAt: string;
  user: OrderUser;
}

export interface TopOrders {
  fetchedAt: string;
  sell: LiveOrder[];
  buy: LiveOrder[];
}

export interface RecentOrder {
  id: string;
  type: 'buy' | 'sell';
  platinum: number;
  quantity: number;
  rank: number | null;
  user_name: string | null;
  user_reputation: number | null;
  user_status: string | null;
  created_at: string;
  slug: string;
  name: string;
  image: string | null;
  category: Category;
}

export interface DucatRow extends ItemSummary {
  ducats_per_plat: number | null;
}

export interface Mover {
  /** null for items without ranks. */
  rank: number | null;
  from: number;
  to: number;
  change: number;
  volume: number;
  item: ItemSummary;
}

export interface Movers {
  gainers: Mover[];
  losers: Mover[];
}

export interface WatchRow extends ItemSummary {
  target_price: number | null;
  added_at: string;
  best_sell: number | null;
  snapshot_at: string | null;
}

export interface ItemQuery {
  q?: string;
  category?: string;
  tag?: string;
  vaulted?: boolean;
  sort?: 'name' | 'ducats' | 'mastery' | 'tax' | 'price';
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

export type JobName = 'backfill' | 'sync-catalog' | 'record';
/** `state` is `status` with crashed jobs (running but no heartbeat) reported as `stalled`. */
export type JobState = 'running' | 'stalled' | 'finished' | 'stopped' | 'failed';

export interface JobDetail {
  /** Backfill: the scope's label, e.g. a category name, "items with history" or "watchlist". */
  scope?: string;
  /** Backfill: items skipped because they were already fetched today (resuming). */
  skipped?: number;
  phase?: 'catalog' | 'item details';
  connection?: 'connecting' | 'connected' | 'reconnecting';
  connected_at?: string;
  last_order_at?: string;
  reconnects?: number;
  snapshots?: number;
  last_snapshot_at?: string;
  online?: { connections: number; authorizedUsers: number };
}

/** One run of a job (the latest), as recorded in the jobs table. */
export interface JobRun {
  status: Exclude<JobState, 'stalled'>;
  state: JobState;
  total: number | null;
  done: number;
  failed: number;
  current: string | null;
  detail: JobDetail;
  message: string | null;
  started_at: string;
  heartbeat_at: string;
  finished_at: string | null;
  stop_requested: boolean;
}

/** A job definition from the backend registry, with its latest run (null if it never ran). */
export interface Job {
  name: JobName;
  title: string;
  /** progress: a known number of items; stream: open-ended, counts events (the recorder). */
  kind: 'progress' | 'stream';
  /** A restart skips work already done. */
  resumable: boolean;
  /** Needs a scope (category or watchlist) to start. */
  scoped: boolean;
  /** What the job counts, e.g. "items" or "orders". */
  unit: string;
  /** Equivalent terminal command. */
  command: string;
  run: JobRun | null;
}

/** A job that has run at least once. */
export type JobWithRun = Job & { run: JobRun };

/** What a backfill started from the dashboard covers: one of these. */
export interface JobStartOptions {
  category?: string;
  slugs?: string[];
  watchlist?: boolean;
  /** Refresh every item that already has history (the daily refresh). */
  with_history?: boolean;
}

// -- Forecasts (backend/wfm/forecast.py) ---------------------------------------

/** One stored forecast: the price `horizon` days after base_date, with an 80% range. */
export interface Forecast {
  horizon: number;
  made_at: string;
  base_date: string;
  base_price: number;
  predicted: number;
  low: number;
  high: number;
  /** model: the LightGBM forecast; baseline: "no change", used where the model did not beat it. */
  method: 'model' | 'baseline';
}

/** A stored forecast for one item at one horizon, with the item it belongs to. */
export interface ItemForecast {
  rank: number | null;
  base_price: number;
  predicted: number;
  low: number;
  high: number;
  /** predicted / base_price - 1 */
  change: number;
  /** predicted - base_price, in platinum (per item bought now and sold at the forecast) */
  gain: number;
  method: Forecast['method'];
  item: ItemSummary;
}

/** How predicted movers are ranked: platinum gained or lost, or percentage change. */
export type MoverRanking = 'gain' | 'change';

export interface PredictedMovers {
  gainers: ItemForecast[];
  losers: ItemForecast[];
}

export interface ForecastSearch {
  /** All matches; `forecasts` holds the first page, sorted by name. */
  total: number;
  forecasts: ItemForecast[];
}

/** Accuracy on the held-out test days. Skill: 1 - model error / "no change" error (above 0 = better). */
export interface ForecastMetrics {
  n: number;
  naive_mae_plat: number;
  trend_skill: number | null;
  model_mae_plat?: number;
  model_mape?: number;
  skill?: number | null;
  direction?: number | null;
  coverage?: number;
  uses_model?: boolean;
}

export interface HorizonReport {
  status: 'tested' | 'not enough data';
  train_rows: number;
  test_rows: number;
  overall: ForecastMetrics;
  /** segment kind ("category" | "band") -> segment value -> metrics */
  segments: Record<string, Record<string, ForecastMetrics>>;
  uses_model: Record<string, Record<string, boolean>>;
}

export interface ForecastReport {
  made_at: string;
  last_date: string;
  cutoff: string;
  test_days: number;
  series: number;
  forecasts: number;
  horizons: Record<string, HorizonReport>;
  feature_importance?: { feature: string; share: number }[];
}
