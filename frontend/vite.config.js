import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// React 설정을 유지하고 개발용 Orchestrator 요청을 전달합니다.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // 요청 경로를 유지한 채 8001번 포트의 Orchestrator로 전달합니다.
      '/api/v1': 'http://127.0.0.1:8001',
      '/health': 'http://127.0.0.1:8001',
    },
  },
})
