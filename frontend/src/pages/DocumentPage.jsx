import { useEffect, useState } from 'react'
import './DocumentPage.css'

const uploadErrorMessages = {
  401: '로그인이 필요합니다.',
  413: '10MiB 이하의 PDF만 제출할 수 있습니다.',
  415: 'PDF 파일만 제출할 수 있습니다.',
  422: '입력값을 확인해주세요.',
  500: '문서 저장 중 서버 오류가 발생했습니다.',
}

async function fetchDocumentList(view, signal) {
  const token = sessionStorage.getItem('access_token')
  const result = { documents: [], errorMessage: '' }
  if (!token) {
    return { ...result, errorMessage: '로그인이 필요합니다.' }
  }

  try {
    const response = await fetch(`http://127.0.0.1:8000/api/documents?view=${view}`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}` },
      signal,
    })
    const data = await response.json().catch(() => null)

    if (response.status === 401) {
      result.errorMessage = '로그인이 필요합니다.'
    } else if (response.status >= 500) {
      result.errorMessage = '문서 목록 조회 중 서버 오류가 발생했습니다.'
    } else if (!response.ok) {
      result.errorMessage = typeof data?.detail === 'string' && data.detail
        ? data.detail : `문서 목록을 조회하지 못했습니다. (HTTP ${response.status})`
    } else if (!Array.isArray(data?.documents)) {
      result.errorMessage = '서버 응답에서 문서 목록을 확인할 수 없습니다.'
    } else {
      result.documents = data.documents
    }
  } catch {
    result.errorMessage = '서버에 연결할 수 없습니다.'
  }

  return result
}

function formatFileSize(bytes) {
  const useMegabytes = bytes >= 1000000
  const size = bytes / (useMegabytes ? 1000000 : 1000)
  return `${size.toLocaleString('ko-KR', { maximumFractionDigits: 2 })} ${useMegabytes ? 'MB' : 'KB'}`
}

function DocumentPage({ onBack }) {
  const [file, setFile] = useState(null)
  const [assigneeId, setAssigneeId] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [uploadResult, setUploadResult] = useState(null)
  const [selectedView, setSelectedView] = useState('submitted')
  const [listVersion, setListVersion] = useState(0)
  const [listResult, setListResult] = useState(null)

  // 이전 탭 또는 갱신 이전의 결과는 현재 목록에 표시하지 않습니다.
  const isLoadingDocuments = listResult?.view !== selectedView || listResult?.version !== listVersion

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    fetchDocumentList(selectedView, controller.signal).then((result) => {
      if (active) {
        setListResult({ ...result, view: selectedView, version: listVersion })
      }
    })

    return () => {
      active = false
      controller.abort()
    }
  }, [selectedView, listVersion])

  function handleViewChange(view) {
    if (view === selectedView) return
    setSelectedView(view)
    setListVersion((version) => version + 1)
  }

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
        // 업로드 중 탭이 바뀌어도 현재 선택된 탭의 목록을 다시 조회합니다.
        setListVersion((version) => version + 1)
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

        <section className="document-card document-history" aria-labelledby="document-history-title">
          <header className="document-history-heading">
            <h2 id="document-history-title">문서 내역</h2>
            <button
              type="button"
              className="app-button"
              disabled={isLoadingDocuments}
              onClick={() => setListVersion((version) => version + 1)}
            >
              목록 새로고침
            </button>
          </header>
          <div className="document-tabs" role="group" aria-label="문서 조회 구분">
            <button
              type="button"
              className="app-button document-tab"
              aria-pressed={selectedView === 'submitted'}
              onClick={() => handleViewChange('submitted')}
            >
              내가 제출한 문서
            </button>
            <button
              type="button"
              className="app-button document-tab"
              aria-pressed={selectedView === 'assigned'}
              onClick={() => handleViewChange('assigned')}
            >
              나에게 배정된 문서
            </button>
          </div>
          <div className="document-list-panel" aria-busy={isLoadingDocuments}>
            {isLoadingDocuments ? (
              <p role="status">문서 목록을 조회하고 있습니다.</p>
            ) : listResult.errorMessage ? (
              <p className="document-message document-message--error" role="alert">
                {listResult.errorMessage}
              </p>
            ) : listResult.documents.length === 0 ? (
              <p role="status">
                {selectedView === 'submitted' ? '제출한 문서가 없습니다.' : '배정된 문서가 없습니다.'}
              </p>
            ) : (
              <ul className="document-list">
                {listResult.documents.map((document) => (
                  <li key={document.id}>
                    <article className="document-list-item">
                      <h3>{document.original_filename}</h3>
                      <dl className="document-result">
                        <dt>문서 ID</dt><dd>{document.id}</dd>
                        <dt>제출 일시</dt>
                        <dd><time dateTime={document.created_at}>{document.created_at.replace('T', ' ')}</time></dd>
                        <dt>파일 크기</dt><dd>{formatFileSize(document.file_size)}</dd>
                        <dt>제출자 ID</dt><dd>{document.submitter_id}</dd>
                        <dt>담당자 ID</dt><dd>{document.assignee_id}</dd>
                      </dl>
                    </article>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </div>
    </main>
  )
}

export default DocumentPage
