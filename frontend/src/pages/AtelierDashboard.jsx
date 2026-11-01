
import { useEffect, useRef, useState } from 'react'
import { createAgentRun } from '../api/agentRuns'

const SIGNUP_SCENARIO_ID = 'f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76'

function AtelierDashboard({ onRunScenario, onOpenDocuments, onLogout }) {
  const iframeRef = useRef(null)
  const requestInProgress = useRef(false)
  const [runMessage, setRunMessage] = useState('')

  useEffect(() => {
    async function handleMessage(event) {
      // 다른 사이트나 다른 창에서 보낸 메시지는 무시
      if (event.origin !== window.location.origin) return
      if (event.source !== iframeRef.current?.contentWindow) return
      if (event.data?.type !== 'atelier:create-run') return

      const requestText = event.data.requestText?.trim()
      if (typeof requestText !== 'string' || !requestText) return
      if (requestInProgress.current) return

      requestInProgress.current = true
      setRunMessage('Agent 실행 요청 중...')

      try {
        const result = await createAgentRun(
          SIGNUP_SCENARIO_ID,
          requestText
        )

        setRunMessage(`Run 생성 성공: ${result.run.runId}`)
      } catch (error) {
        setRunMessage(`실행 요청 실패: ${error.message}`)
      } finally {
        requestInProgress.current = false
      }
    }

    window.addEventListener('message', handleMessage)

    return () => {
      window.removeEventListener('message', handleMessage)
    }
  }, [])

  
return (
  <main style={{ width: '100%', minHeight: '100svh' }}>
    <iframe
      ref={iframeRef}
      src="/atelier-preview.html#/home"
      title="atelier 오피스 디자인"
      style={{
        width: '100%',
        height: '100svh',
        border: 'none',
        display: 'block',
      }}
    />

    {runMessage && (
      <div
        role="status"
        style={{
          position: 'fixed',
          right: '24px',
          bottom: '24px',
          zIndex: 100,
          padding: '12px 18px',
          maxWidth: '420px',
          borderRadius: '10px',
          background: '#242a32',
          color: '#fff',
          overflowWrap: 'anywhere',
        }}
      >
        {runMessage}
      </div>
    )}
  </main>
)
}

export default AtelierDashboard
