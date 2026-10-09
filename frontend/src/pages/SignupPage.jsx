import { useState } from 'react'

function SignupPage({ onBack, onSignupSuccess }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [message, setMessage] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (isSubmitting) return

    // 비밀번호 확인은 화면에서만 검사하고, 기존 API 요청 형식은 유지합니다.
    if (password !== confirmPassword) {
      setMessage('비밀번호가 일치하지 않습니다.')
      return
    }

    setIsSubmitting(true)
    setMessage('')

    try {
      const response = await fetch('http://127.0.0.1:8000/api/auth/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      const data = await response.json()

      if (response.ok) {
        setMessage(data.message || '회원가입에 성공했습니다.')
        onSignupSuccess(data.message || '회원가입에 성공했습니다.')
      } else if (Array.isArray(data.detail)) {
        const errorMessage = data.detail
          .map((error) => error.msg.replace(/^Value error, /, ''))
          .join(' ')
        setMessage(errorMessage || '회원가입에 실패했습니다.')
      } else {
        setMessage(data.detail || data.message || '회원가입에 실패했습니다.')
      }
    } catch {
      setMessage('서버에 연결할 수 없습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <main className="auth-page">
      <div className="auth-shell">
        <div className="product-brand auth-brand">
          <span className="brand-mark" aria-hidden="true">A</span>
          <span>A2A Agent Company</span>
        </div>
        <section className="auth-card" aria-labelledby="signup-title">
          <header className="auth-heading">
            <h1 id="signup-title">계정 만들기</h1>
            <p>계정을 만들고 AI Agent 협업을 시작하세요.</p>
          </header>
          <form className="signup-form" onSubmit={handleSubmit} noValidate>
            <div className="signup-field">
              <label htmlFor="signup-email">이메일</label>
              <input
                id="signup-email"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="name@example.com"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
            <div className="signup-field">
              <label htmlFor="signup-password">비밀번호</label>
              <input
                id="signup-password"
                name="password"
                type="password"
                autoComplete="new-password"
                placeholder="최소 8자 이상 입력"
                aria-describedby="password-hint"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <p id="password-hint">비밀번호는 최소 8자 이상이어야 합니다.</p>
            </div>
            <div className="signup-field">
              <label htmlFor="signup-password-confirm">비밀번호 확인</label>
              <input
                id="signup-password-confirm"
                name="confirm-password"
                type="password"
                autoComplete="new-password"
                placeholder="비밀번호를 다시 입력해주세요"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
              />
            </div>
            {message && <p className="auth-message" role="status">{message}</p>}
            <div className="signup-actions">
              <button type="submit" className="app-button app-button--primary" disabled={isSubmitting}>
                {isSubmitting ? '처리 중...' : '회원가입'}
              </button>
            </div>
          </form>
          <p className="auth-switch">
            이미 계정이 있으신가요?
            <button type="button" className="text-button" onClick={onBack} disabled={isSubmitting}>로그인</button>
          </p>
        </section>
        <p className="auth-footer">AI Agent 협업을 위한 업무 공간</p>
      </div>
    </main>
  )
}

export default SignupPage
