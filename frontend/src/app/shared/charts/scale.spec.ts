import { bandPath, columnPath, linePath, linear, nearestIndex, niceTicks, spacedIndexes, tooltipLeft } from './scale';

describe('chart scales', () => {
  it('maps a domain onto a range', () => {
    const s = linear([0, 10], [0, 100]);
    expect(s(5)).toBe(50);
  });

  it('produces round ticks covering the data', () => {
    const ticks = niceTicks(7, 132, 4);
    expect(ticks[0]).toBeLessThanOrEqual(7);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(132);
    expect(ticks).toEqual([0, 50, 100, 150]);
  });

  it('handles a flat series', () => {
    const ticks = niceTicks(10, 10);
    expect(ticks.length).toBeGreaterThan(1);
  });

  it('breaks lines at missing values', () => {
    const id = (v: number) => v;
    const d = linePath([{ t: 0, v: 1 }, { t: 1, v: null }, { t: 2, v: 3 }], id, id);
    expect(d).toBe('M0.0,1.0M2.0,3.0');
  });

  it('skips zero-height columns', () => {
    expect(columnPath(0, 100, 10, 100)).toBe('');
    expect(columnPath(0, 50, 10, 100)).toContain('Q');
  });

  it('finds the nearest timestamp', () => {
    expect(nearestIndex([0, 10, 20], 14)).toBe(1);
    expect(nearestIndex([0, 10, 20], 16)).toBe(2);
  });

  it('thins axis labels', () => {
    expect(spacedIndexes(10, 4)).toEqual([0, 3, 6, 9]);
    expect(spacedIndexes(3, 4)).toEqual([0, 1, 2]);
  });

  it('flips the tooltip before it overflows the right edge', () => {
    expect(tooltipLeft(100, 600)).toBe(112);
    expect(tooltipLeft(500, 600)).toBe(340);
  });

  it('draws a closed band: highs left to right, then lows back', () => {
    const id = (v: number) => v;
    expect(bandPath([{ t: 0, low: 1, high: 3 }, { t: 2, low: 2, high: 6 }], id, id)).toBe('M0.0,3.0L2.0,6.0L2.0,2.0L0.0,1.0Z');
    expect(bandPath([{ t: 0, low: 1, high: 3 }], id, id)).toBe('');
  });
});
