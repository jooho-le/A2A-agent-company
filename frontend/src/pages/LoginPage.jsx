import { useState } from 'react'
import googleIcon from '../assets/google.svg'
import githubIcon from '../assets/github.svg'
import './LoginPage.css'

function LoginPage({ onSignup, onLoginSuccess, notice = '' }) {
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
    <main className="auth-page login-page">
      <div className="auth-shell">
        <div className="product-brand auth-brand">
          <span className="brand-mark" aria-hidden="true">A</span>
          <span>A2A Agent Company</span>
        </div>
        <section className="auth-card" aria-labelledby="login-title">
          <header className="auth-heading">
            <h1 id="login-title">로그인</h1>
            <p>AI Agent 협업 서비스를 이용하려면 로그인하세요.</p>
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
            <div className="auth-options">
              <label className="remember-option">
                <input type="checkbox" disabled />
                <span>로그인 상태 유지 <small>준비 중</small></span>
              </label>
              <button type="button" className="text-button" disabled>비밀번호를 잊으셨나요? (준비 중)</button>
            </div>
            {message && <p className="auth-message" role="status">{message}</p>}
            <div className="login-actions">
              <button type="submit" className="app-button app-button--primary" disabled={isSubmitting}>
                {isSubmitting ? '처리 중...' : '로그인'}
              </button>
            </div>
          </form>
          <div className="auth-divider"><span>또는</span></div>
          <div className="social-actions">
            <button type="button" className="app-button social-button" disabled>
              <img className="social-symbol" src={googleIcon} alt="" aria-hidden="true" />
              <span>Google로 계속하기</span><small>준비 중</small>
            </button>
            <button type="button" className="app-button social-button" disabled>
              <img className="social-symbol" src={githubIcon} alt="" aria-hidden="true" />
              <span>GitHub로 계속하기</span><small>준비 중</small>
            </button>
          </div>
          <p className="auth-switch">
            계정이 없으신가요?
            <button type="button" className="text-button" onClick={onSignup} disabled={isSubmitting}>회원가입</button>
          </p>
        </section>
        <p className="auth-footer">AI Agent 협업을 위한 업무 공간</p>
      </div>
    </main>
  )
}

export default LoginPage
