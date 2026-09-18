const FPS = 30;

export function sourceFrame(item, time) {
  return Math.max(0, Math.floor((time - item.start + item.mediaStart) * FPS + 1e-6));
}

export function frameWindow(item, duration) {
  if (![item.start, item.duration, item.mediaStart, duration].every(Number.isFinite)
      || item.start < 0 || item.duration <= 0 || item.mediaStart < 0 || duration <= 0) {
    throw Error('Invalid source timeline');
  }
  const first = Math.max(0, Math.ceil(item.start * FPS - 1e-6));
  const end = Math.min(Math.round(duration * FPS), Math.ceil((item.start + item.duration) * FPS - 1e-6));
  if (first >= end) return null;
  return item.image ? {start: 0, end: 1}
    : {start: sourceFrame(item, first / FPS), end: sourceFrame(item, (end - 1) / FPS) + 1};
}

export function mergeWindows(windows) {
  const merged = [];
  for (const window of windows.filter(Boolean).sort((a, b) => a.start - b.start)) {
    if (!Number.isSafeInteger(window.start) || !Number.isSafeInteger(window.end)
        || window.start < 0 || window.end <= window.start) throw Error('Invalid decode window');
    const last = merged.at(-1);
    if (last && window.start <= last.end) last.end = Math.max(last.end, window.end);
    else merged.push({...window});
  }
  return merged;
}
