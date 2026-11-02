/** Monotonic offsets share one axis; parallel spans are never added together. */
export function timeline(stage: {start_s: number; end_s: number | null}, elapsed: number): {left: number; width: number; seconds: number} {
  const end = stage.end_s ?? elapsed;
  return {left: 100 * stage.start_s / Math.max(elapsed, .001),
    width: Math.max(.25, 100 * Math.max(0, end - stage.start_s) / Math.max(elapsed, .001)),
    seconds: Math.max(0, end - stage.start_s)};
}
