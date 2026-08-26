// Loading placeholder — flat bars on --border, canvas widths 30% / 100% / 61%.
const WIDTHS = ["30%", "100%", "61%", "84%", "45%"];

export default function Skeleton({ lines = 3 }) {
  return (
    <div data-testid="skeleton" className="flex animate-pulse flex-col gap-2" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="h-2.5 rounded-[4px] bg-border"
          style={{ width: WIDTHS[i % WIDTHS.length] }}
        />
      ))}
    </div>
  );
}
