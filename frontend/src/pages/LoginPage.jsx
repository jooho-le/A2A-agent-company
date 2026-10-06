import { useState } from 'react'

function LoginPage({ onBack }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)

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
    <main className="login">
      <h1>로그인</h1>
      <form className="login-form" onSubmit={handleSubmit} noValidate>
        <div className="login-field">
          <label htmlFor="login-email">이메일</label>
          <input
            id="login-email"
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <div className="login-field">
          <label htmlFor="login-password">비밀번호</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <p role="status">{message}</p>
        <div className="login-actions">
          <button type="submit" className="app-button" disabled={isSubmitting}>
            {isSubmitting ? '처리 중...' : '로그인'}
          </button>
          <button type="button" className="app-button" onClick={onBack}>
            처음 화면으로 돌아가기
          </button>
        </div>
      </form>
    </main>
  )
}

export default LoginPage
