import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

const here = path.dirname(fileURLToPath(import.meta.url))

// The built page lives in the site's own assets folder, so Flask serves it as
// /assets/intro/index.html next to everything else. base './' keeps every link relative.
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  build: { outDir: path.resolve(here, '../assets/intro'), emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  server: { proxy: { '/api': 'http://127.0.0.1:5000' } },   // `npm run dev` talks to the Flask app
})
