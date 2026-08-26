// Inline [n] citation chip inside answer text — click jumps to its SourceCard.
export default function CitationChip({ n, onClick }) {
  return (
    <button
      type="button"
      data-testid="citation-chip"
      data-n={n}
      onClick={() => onClick(n)}
      aria-label={`Go to source ${n}`}
      className="mx-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-control bg-accent-soft px-1.5 align-middle font-mono text-[11px] font-semibold text-accent hover:bg-accent hover:text-surface"
    >
      {n}
    </button>
  );
}
