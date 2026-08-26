// 11px uppercase label over a 24/600 tabular figure, optional third line
// (tone "positive" for deltas like "+2 today", "muted" for meta).
export default function StatCard({ label, value, hint, tone = "muted", testId }) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-1.5 rounded-card border border-border bg-surface p-5 shadow-card"
    >
      <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3">
        {label}
      </div>
      <div className="truncate text-2xl font-semibold text-text">{value}</div>
      {hint != null ? (
        <div
          className={
            tone === "positive"
              ? "text-xs font-medium text-positive"
              : "text-xs text-text-3"
          }
        >
          {hint}
        </div>
      ) : null}
    </div>
  );
}
