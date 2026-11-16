import react from '@vitejs/plugin-react'
import process from 'node:process'
import { defineConfig } from 'vite'

// Vite 프로세스에만 설정하는 제품 서버 주소입니다. Host로 JWT를 직접 보내지 않습니다.
const productTarget = process.env.PRODUCT_API_PROXY_TARGET || 'http://127.0.0.1:8000'

// React 설정을 유지하고 개발용 제품 API 요청을 전달합니다.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // 로그인·프로젝트·실행 요청을 같은 제품 서버로 전달합니다.
      '/api': productTarget,
    },
  },
})
