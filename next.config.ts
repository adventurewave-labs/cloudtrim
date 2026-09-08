import type { NextConfig } from "next";

const engineUrl = process.env.ENGINE_API_URL || "http://127.0.0.1:3030";

const nextConfig: NextConfig = {
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  reactStrictMode: false,
  // In docker-compose the browser reaches the Next.js server directly, so
  // /api/* is proxied to the CloudTrim engine container. In the sandbox the
  // Caddy gateway routes ?XTransformPort=3030 to the engine instead.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${engineUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
