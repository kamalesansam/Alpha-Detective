// Shared display formatters — canvas-faithful ("68 KB", "Aug 25, 09:14",
// "Today 09:14", "118 ms" / "1.9 s").

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function pad(n) {
  return String(n).padStart(2, "0");
}

export function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

/** "Aug 25, 09:14" (local time) */
export function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "Today 09:14" when today, else "Aug 18, 16:40" */
export function formatDayTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  if (isToday(iso)) return `Today ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return formatDateTime(iso);
}

/** "09:21" */
export function formatClock(date) {
  const d = date instanceof Date ? date : new Date(date);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "118 ms" below one second, "1.9 s" from there up */
export function formatMs(ms) {
  if (ms == null || !Number.isFinite(ms)) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function extLabel(ext) {
  return String(ext || "").replace(/^\./, "").toUpperCase();
}

/** "1 chunk" / "3 chunks" — naive s-pluralization is fine for our nouns. */
export function plural(n, noun) {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

export function isToday(iso) {
  if (!iso) return false;
  const d = new Date(iso);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}
