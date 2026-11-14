import { svelte } from '@sveltejs/vite-plugin-svelte'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// The payroll engine (Rust) is reached through /engine in development so the
// browser never needs its address; production puts the same prefix on the
// reverse proxy.
export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  server: {
    proxy: {
      '/engine': {
        target: process.env.ENGINE_URL ?? 'http://127.0.0.1:8090',
        rewrite: (path) => path.replace(/^\/engine/, ''),
      },
    },
  },
})
