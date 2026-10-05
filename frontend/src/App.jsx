import { useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import './App.css'

function App() {
  const [showSignup, setShowSignup] = useState(false)
  const [showLogin, setShowLogin] = useState(false)

  if (showSignup) {
    return <SignupPage onBack={() => setShowSignup(false)} />
  }

  if (showLogin) {
    return <LoginPage onBack={() => setShowLogin(false)} />
  }

  return (
    <main className="intro">
      <h1>A2A Multi-Agent Demo</h1>
      <p>회원가입 및 전자문서 제출·조회 서비스</p>
      <div className="intro-actions">
        <button
          type="button"
          className="app-button"
          onClick={() => setShowSignup(true)}
        >
          회원가입
        </button>
        <button
          type="button"
          className="app-button"
          onClick={() => setShowLogin(true)}
        >
          로그인
        </button>
      </div>
    </main>
  )
}

export default App
