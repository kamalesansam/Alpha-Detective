/** @type {import('next').NextConfig} */

// CONTRACTS §4.1 / §5.1: the rewrite destination is server-side configuration,
// never a client value. Vercel sets BACKEND_ORIGIN to the Render service URL;
// local dev falls back to the uvicorn default. Deliberately NOT NEXT_PUBLIC_*
// — the browser only ever sees same-origin /api/... paths.
const BACKEND_ORIGIN = (process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

const nextConfig = {
  // Dev-tools "N" indicator occluded the StatusPill in review screenshots
  // (design round1 MAJOR-2) — dev mode must match product UI.
  devIndicators: false,
  // Single origin in the browser: every /api/* request is proxied to FastAPI
  // (CLAUDE_CODE_PROMPT.md §5, CONTRACTS §1). No host is ever hardcoded in app code.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
