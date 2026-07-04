import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  env: {
    PADDLEDOC_PUBLIC_API_URL: process.env.PADDLEDOC_PUBLIC_API_URL,
  },
};

export default nextConfig;
