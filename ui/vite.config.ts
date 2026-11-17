/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The bundled sample imports the shared mock fixture and its source file
  // from tests/fixtures, outside this package.
  server: { fs: { allow: [".."] } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
