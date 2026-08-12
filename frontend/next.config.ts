import type { NextConfig } from "next";

const config: NextConfig = {
  // The API is a separate process (uvicorn on :8000). Proxying it under the same
  // origin keeps CORS out of the browser's way for everything except the CSV
  // download, which needs its Content-Disposition header read directly.
  //
  // **`API_URL` is read at build time, not per request.** `next build` evaluates
  // this function and writes the destination into `.next/routes-manifest.json`;
  // `next start` serves from that manifest and never calls it again. It only
  // looks dynamic because `next dev` re-evaluates the config on change. So a
  // container that sets API_URL at runtime only will proxy to the fallback below
  // and give ECONNREFUSED against a perfectly healthy API — which is why
  // frontend/Dockerfile passes it as an ARG.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.API_URL ?? "http://127.0.0.1:8000"}/api/:path*`,
      },
    ];
  },
};

export default config;
