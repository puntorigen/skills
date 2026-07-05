import { defineConfig } from 'vite';

export default defineConfig({
  resolve: {
    dedupe: ['three'],
  },
  optimizeDeps: {
    include: ['three', '@mkkellogg/gaussian-splats-3d'],
  },
});
