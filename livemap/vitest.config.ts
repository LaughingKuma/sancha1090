import { defineConfig } from "vitest/config";

// the specs sit outside the Vite root (tests/js), so the JSX transform and the fs allowance are pinned
// here rather than discovered from tsconfig.json
export default defineConfig({
  oxc: { jsx: { runtime: "automatic", importSource: "preact" } },
  server: { fs: { allow: [".."] } },
  test: {
    environment: "happy-dom",
    include: ["../tests/js/**/*.test.ts", "../tests/js/**/*.test.tsx"],
  },
});
