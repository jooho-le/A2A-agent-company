import { useState } from 'react'

function SignupPage({ onBack }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  function handleSubmit(event) {
    event.preventDefault()
  }

  return (
    <main className="signup">
      <h1>회원가입</h1>
      <form className="signup-form" onSubmit={handleSubmit}>
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
        <div className="signup-actions">
          <button type="submit" className="app-button">회원가입</button>
          <button type="button" className="app-button" onClick={onBack}>
            처음 화면으로 돌아가기
          </button>
        </div>
      </form>
    </main>
  )
}

export default SignupPage
