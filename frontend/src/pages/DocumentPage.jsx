import { useState } from 'react'
import './DocumentPage.css'

const uploadErrorMessages = {
  401: '로그인이 필요합니다.',
  413: '10MiB 이하의 PDF만 제출할 수 있습니다.',
  415: 'PDF 파일만 제출할 수 있습니다.',
  422: '입력값을 확인해주세요.',
  500: '문서 저장 중 서버 오류가 발생했습니다.',
}

function DocumentPage({ onBack }) {
  const [file, setFile] = useState(null)
  const [assigneeId, setAssigneeId] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [uploadResult, setUploadResult] = useState(null)

  async function handleSubmit(event) {
    event.preventDefault()
    if (isSubmitting) return

    setErrorMessage('')
    setUploadResult(null)

    if (!file) {
      setErrorMessage('PDF 파일을 선택해주세요.')
      return
    }
    if (!assigneeId.trim()) {
      setErrorMessage('담당자 ID를 입력해주세요.')
      return
    }

    const token = sessionStorage.getItem('access_token')
    if (!token) {
      setErrorMessage('로그인이 필요합니다.')
      return
    }

    // 파일 크기와 PDF 여부의 최종 검증은 기존 백엔드에서 수행합니다.
    const formData = new FormData()
    formData.append('file', file)
    formData.append('assignee_id', assigneeId.trim())
    setIsSubmitting(true)

    try {
      const response = await fetch('http://127.0.0.1:8000/api/documents', {
        method: 'POST',
        // multipart boundary는 브라우저가 생성하므로 Content-Type을 지정하지 않습니다.
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      })
      const data = await response.json().catch(() => null)

      if (response.status === 201) {
        if (!Number.isInteger(data?.id) || typeof data?.original_filename !== 'string'
          || !Number.isInteger(data?.file_size) || !Number.isInteger(data?.assignee_id)) {
          setErrorMessage('서버 응답에서 제출 결과를 확인할 수 없습니다.')
          return
        }
        setUploadResult(data)
      } else {
        const detail = typeof data?.detail === 'string' ? data.detail : ''
        setErrorMessage(
          uploadErrorMessages[response.status] || detail
            || `문서 제출에 실패했습니다. (HTTP ${response.status})`,
        )
      }
    } catch {
      setErrorMessage('서버에 연결할 수 없습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <main className="dashboard-page document-page">
      <header className="dashboard-header">
        <div className="product-brand">
          <span className="brand-mark" aria-hidden="true">A</span>
          <span>A2A Agent Company</span>
        </div>
        <button type="button" className="app-button" onClick={onBack} disabled={isSubmitting}>
          Dashboard로 돌아가기
        </button>
      </header>

      <div className="dashboard-content document-content">
        <header className="dashboard-intro">
          <p className="eyebrow">시나리오 2</p>
          <h1>전자문서 제출</h1>
          <p>PDF 문서를 선택하고 담당자 ID를 입력하여 제출해주세요.</p>
        </header>

        <form className="document-card document-form" onSubmit={handleSubmit} noValidate aria-busy={isSubmitting}>
          <div className="document-field">
            <label htmlFor="document-file">PDF 파일</label>
            <input
              id="document-file"
              name="file"
              type="file"
              accept=".pdf,application/pdf"
              aria-describedby="document-file-hint"
              required
              disabled={isSubmitting}
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
            <p id="document-file-hint">PDF 파일 1개 · 최대 10MiB</p>
          </div>
          <div className="document-field">
            <label htmlFor="document-assignee">담당자 ID</label>
            <input
              id="document-assignee"
              name="assignee_id"
              type="number"
              min="1"
              step="1"
              placeholder="사용자 ID 입력"
              aria-describedby="document-assignee-hint"
              required
              value={assigneeId}
              disabled={isSubmitting}
              onChange={(event) => setAssigneeId(event.target.value)}
            />
            <p id="document-assignee-hint">기존 가입 사용자의 ID를 입력해주세요.</p>
          </div>

          {isSubmitting && <p role="status">문서를 제출하고 있습니다.</p>}
          {errorMessage && <p className="document-message document-message--error" role="alert">{errorMessage}</p>}
          {uploadResult && (
            <section className="document-message document-message--success" aria-labelledby="document-success-title">
              <h2 id="document-success-title" role="status">문서가 성공적으로 제출되었습니다.</h2>
              <dl className="document-result">
                <dt>문서 ID</dt><dd>{uploadResult.id}</dd>
                <dt>원본 파일명</dt><dd>{uploadResult.original_filename}</dd>
                <dt>파일 크기</dt><dd>{uploadResult.file_size.toLocaleString('ko-KR')} bytes</dd>
                <dt>담당자 ID</dt><dd>{uploadResult.assignee_id}</dd>
              </dl>
            </section>
          )}
          <button type="submit" className="app-button app-button--primary" disabled={isSubmitting}>
            {isSubmitting ? '제출 중...' : '문서 제출'}
          </button>
        </form>
      </div>
    </main>
  )
}

export default DocumentPage
