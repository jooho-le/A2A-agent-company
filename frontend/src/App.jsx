import { useState } from 'react'
import SignupPage from './pages/SignupPage'
import LoginPage from './pages/LoginPage'
import PipelinePage from './pages/PipelinePage'
import './App.css'

function App() {
  const [showSignup, setShowSignup] = useState(false)
  const [showLogin, setShowLogin] = useState(false)
  const [showPipeline, setShowPipeline] = useState(false)

  if (showSignup) {
    return <SignupPage onBack={() => setShowSignup(false)} />
  }

  if (showLogin) {
    return <LoginPage onBack={() => setShowLogin(false)} />
  }

  if (showPipeline) {
    return <PipelinePage onBack={() => setShowPipeline(false)} />
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
        <button
          type="button"
          className="app-button"
          onClick={() => setShowPipeline(true)}
        >
          시나리오 실행
        </button>
      </div>
    </main>
  )
}

export default App
