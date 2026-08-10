import type { NextConfig } from "next";

const config: NextConfig = {
  // The API is a separate process (uvicorn on :8000). Proxying it under the same
  // origin keeps CORS out of the browser's way for everything except the CSV
  // download, which needs its Content-Disposition header read directly.
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
