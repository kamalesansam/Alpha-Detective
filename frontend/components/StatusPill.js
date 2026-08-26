// Provider status pill, pinned at the bottom of the sidebar (CONTRACTS §4.2).
export default function StatusPill({ health, offline }) {
  let bg, dot, text, label;
  if (offline) {
    bg = "bg-negative-soft";
    dot = "bg-negative";
    text = "text-negative";
    label = "Backend offline";
  } else if (health?.provider === "gemini") {
    bg = "bg-positive-soft";
    dot = "bg-positive";
    text = "text-positive";
    label = "Gemini connected";
  } else if (health?.provider === "none") {
    bg = "bg-warning-soft";
    dot = "bg-warning";
    text = "text-warning";
    label = "Retrieval-only mode";
  } else {
    bg = "bg-bg";
    dot = "bg-border-strong";
    text = "text-text-3";
    label = "Connecting…";
  }

  return (
    <span
      data-testid="provider-pill"
      className={`inline-flex h-7 items-center gap-2 rounded-control px-2.5 ${bg}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden="true" />
      <span className={`text-xs font-medium ${text}`}>{label}</span>
    </span>
  );
}
