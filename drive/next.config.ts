import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `standalone` bundles only the modules actually imported by the server, so
  // the Docker image does not carry the whole dependency tree. It does not
  // change what Netlify deploys — the plugin reads the normal `.next` output.
  output: "standalone",

  // The captured UI is served as raw bytes from public/drive-ui/, so Next never
  // parses or rewrites it. Nothing here may transform that HTML.
  reactStrictMode: true,
};

export default nextConfig;