import { useEffect, useRef, useState } from 'react'
import { downloadOfficeSource } from '../../api/officeService.js'
import { Icon } from './AtelierArtwork'

export default function SourceArtifactDownload({ item }) {
  const active = useRef(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)
  useEffect(() => () => active.current?.abort(), [])
  const ready = item.source && Number.isSafeInteger(item.codeVersion) && item.codeVersion > 0
  const unavailable = [403, 404, 409, 413].includes(error?.status)
  const download = async () => {
    if (!ready || active.current || unavailable) return
    const controller = new AbortController()
    active.current = controller
    setPending(true)
    setError(null)
    try {
      const { blob, filename } = await downloadOfficeSource(item.runId, item.artifactId, item.codeVersion, { signal: controller.signal })
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      try { link.click() } finally {
        link.remove()
        // 브라우저가 저장을 시작할 시간을 주고 임시 Blob URL을 정리합니다.
        setTimeout(() => URL.revokeObjectURL(url), 1000)
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError({
        message: failure.name === 'AgentRunApiError' ? failure.message : '파일을 저장하지 못했습니다. 다시 시도해 주세요.',
        status: failure.status,
      })
    } finally {
      if (!controller.signal.aborted) setPending(false)
      active.current = null
    }
  }
  return <>
    <div className="output-actions"><button type="button" className="button" disabled={!ready || pending || unavailable}
      onClick={download}><Icon name="down" />{pending ? 'ZIP 확인 중…' : !ready || unavailable ? '다운로드 불가' : '소스 ZIP 다운로드'}</button></div>
    {!ready && <p>코드 버전 확인이 필요해요.</p>}
    {error && <p role="alert">{error.message}</p>}
  </>
}
