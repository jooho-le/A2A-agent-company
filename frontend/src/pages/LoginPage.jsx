import { useState } from 'react'
import OfficeAuthLayout from '../components/atelier/OfficeAuthLayout'

function LoginPage({ onSignup, onLoginSuccess, onBack, notice = '' }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState(notice)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (isSubmitting) return

    setIsSubmitting(true)
    setMessage('')

    try {
      const response = await fetch('http://127.0.0.1:8000/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      const data = await response.json()

      if (response.ok) {
        if (typeof data.access_token !== 'string' || !data.access_token.trim()) {
          setMessage('로그인 응답을 확인할 수 없습니다. 다시 시도해 주세요.')
          return
        }
        sessionStorage.setItem('access_token', data.access_token)
        setMessage('로그인에 성공했습니다.')
        onLoginSuccess()
      } else if (Array.isArray(data.detail)) {
        const errorMessage = data.detail
          .map((error) => error.msg.replace(/^Value error, /, ''))
          .join(' ')
        setMessage(errorMessage || '로그인에 실패했습니다.')
      } else {
        setMessage(data.detail || '로그인에 실패했습니다.')
      }
    } catch {
      setMessage('서버에 연결할 수 없습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <OfficeAuthLayout>
        <section className="auth-card" aria-labelledby="login-title">
          <header className="auth-heading">
            <p className="office-auth-form-label">대표님 계정</p>
            <h1 id="login-title">로그인</h1>
            <p>우리 회사의 업무를 이어서 시작하세요.</p>
          </header>
          <form className="login-form" onSubmit={handleSubmit} noValidate>
            <div className="login-field">
              <label htmlFor="login-email">이메일</label>
              <input
                id="login-email"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="name@example.com"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
            <div className="login-field">
              <label htmlFor="login-password">비밀번호</label>
              <div className="password-input">
                <input
                  id="login-password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  placeholder="********"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
                <button
                  type="button"
                  className="password-toggle"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? '비밀번호 숨기기' : '비밀번호 표시하기'}
                  aria-pressed={showPassword}
                >
                  {showPassword ? '숨김' : '표시'}
                </button>
              </div>
            </div>
            {message && <p className="auth-message" role="status">{message}</p>}
            <div className="login-actions">
              <button type="submit" className="app-button app-button--primary" disabled={isSubmitting}>
                {isSubmitting ? '처리 중...' : '로그인'}
              </button>
            </div>
          </form>
          <p className="auth-switch">
            계정이 없으신가요?
            <button type="button" className="text-button" onClick={onSignup} disabled={isSubmitting}>회원가입</button>
          </p>
          {onBack && <p className="auth-switch"><button type="button" className="text-button" onClick={onBack} disabled={isSubmitting}>오피스로 돌아가기</button></p>}
        </section>
    </OfficeAuthLayout>
  )
}

export default LoginPage
