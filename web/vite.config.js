import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import { viteSingleFile } from 'vite-plugin-singlefile'

// 成果物は単一 HTML。chist-server は `/` で index.html を返すだけでよく、
// 静的ファイル用のルーティングを持たずに済む。
export default defineConfig({
  plugins: [svelte(), viteSingleFile()],
  build: {
    target: 'es2022',
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,
    chunkSizeWarningLimit: 4096,
  },
  server: {
    // 開発時は起動中の chist-server へ API を転送する
    proxy: { '/api': 'http://127.0.0.1:8848' },
  },
})
