export const TRACK_ALLOCATION = Object.freeze({
  layoutInternals: Object.freeze({start: 1, end: 18}),
  overlayInstances: Object.freeze({start: 32, end: 47}),
  safeAreaHosts: Object.freeze({start: 64, end: 69}),
});

export function overlayTrackIndex(index) {
  return allocated(index, TRACK_ALLOCATION.overlayInstances, "overlay_track_index_invalid");
}

export function safeAreaHostTrackIndex(index) {
  return allocated(index, TRACK_ALLOCATION.safeAreaHosts, "safe_host_track_index_invalid");
}

function allocated(index, range, code) {
  if (!Number.isInteger(index) || index < 0 || range.start + index > range.end) throw new Error(code);
  return range.start + index;
}
