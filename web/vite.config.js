import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import { viteSingleFile } from 'vite-plugin-singlefile'

// The build output is a single HTML file, so the server only has to return
// index.html at `/` and needs no routing for static assets.
export default defineConfig({
  plugins: [svelte(), viteSingleFile()],
  build: {
    target: 'es2022',
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,
    chunkSizeWarningLimit: 4096,
  },
  server: {
    // In development, proxy API calls to a running chist-server
    proxy: { '/api': 'http://127.0.0.1:8848' },
  },
})
