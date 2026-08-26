/** @type {import('next').NextConfig} */
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
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
