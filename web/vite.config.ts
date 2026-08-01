import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Host ports stay inside Sentinel's allocated 8040–8049 block and bind to
// loopback only — this box is shared. 8041 belongs to the Caddy front door
// that serves the production build, so the dev server and preview take the
// spare ports at the top of the block.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 8048,
    strictPort: true,
    // In development the app talks to the gateway directly; in production
    // Caddy does the same proxying at the front door.
    proxy: {
      '/api': 'http://127.0.0.1:8040',
      '/stream': { target: 'http://127.0.0.1:8040', changeOrigin: false },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 8049,
    strictPort: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
    // `e2e/` belongs to Playwright: those specs drive a real browser against a
    // running stack, and jsdom has neither. Running them here would fail for
    // reasons that say nothing about the code.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
});
