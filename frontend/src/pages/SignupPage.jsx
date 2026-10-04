import { useState } from 'react'

function SignupPage({ onBack }) {
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
      const response = await fetch('http://127.0.0.1:8000/api/auth/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      const data = await response.json()

      if (response.ok) {
        setMessage(data.message || '회원가입에 성공했습니다.')
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
    <main className="signup">
      <h1>회원가입</h1>
      <form className="signup-form" onSubmit={handleSubmit} noValidate>
        <div className="signup-field">
          <label htmlFor="signup-email">이메일</label>
          <input
            id="signup-email"
            name="email"
            type="email"
            autoComplete="email"
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
            aria-describedby="password-hint"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <p id="password-hint">비밀번호는 최소 8자 이상이어야 합니다.</p>
        </div>
        {message && <p role="status">{message}</p>}
        <div className="signup-actions">
          <button type="submit" className="app-button" disabled={isSubmitting}>
            {isSubmitting ? '처리 중...' : '회원가입'}
          </button>
          <button type="button" className="app-button" onClick={onBack}>
            처음 화면으로 돌아가기
          </button>
        </div>
      </form>
    </main>
  )
}

export default SignupPage
