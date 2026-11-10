import react from '@vitejs/plugin-react'
import process from 'node:process'
import { defineConfig } from 'vite'

// Vite 실행 환경에서만 읽습니다. 미지정 시 기존 Orchestrator를 사용합니다.
const orchestratorTarget = process.env.ORCHESTRATOR_PROXY_TARGET || 'http://127.0.0.1:8001'

// React 설정을 유지하고 개발용 Orchestrator 요청을 전달합니다.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // API와 상태 확인 요청을 같은 Orchestrator로 전달합니다.
      '/api/v1': orchestratorTarget,
      '/health': orchestratorTarget,
    },
  },
})
