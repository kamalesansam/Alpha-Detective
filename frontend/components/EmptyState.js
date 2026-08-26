// Bordered empty state: one sentence, one primary button (§8).
export default function EmptyState({ title, message, actionLabel, onAction, testId }) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col items-center gap-3 rounded-card border border-border bg-surface px-6 py-10 text-center shadow-card"
    >
      <div className="text-sm font-medium text-text">{title}</div>
      <p className="text-[13px] leading-normal text-text-2">{message}</p>
      {actionLabel && onAction ? (
        <button
          type="button"
          onClick={onAction}
          className="mt-1 inline-flex h-9 items-center rounded-control bg-accent px-4 text-sm font-medium text-surface hover:bg-accent-hover"
        >
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
}
