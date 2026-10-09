function DashboardPage({ onRunScenario, onLogout }) {
  return (
    <main className="dashboard-page">
      <header className="dashboard-header">
        <div className="product-brand">
          <span className="brand-mark" aria-hidden="true">A</span>
          <span>A2A Agent Company</span>
        </div>
        <button type="button" className="app-button" onClick={onLogout}>로그아웃</button>
      </header>

      <div className="dashboard-content">
        <header className="dashboard-intro">
          <p className="eyebrow">A2A Dashboard</p>
          <h1>시나리오 선택</h1>
          <p>안녕하세요. 실행할 AI Agent 협업 시나리오를 선택하세요.</p>
        </header>

        <section className="scenario-grid" aria-label="협업 시나리오 목록">
          <article className="scenario-card">
            <div className="scenario-card-heading">
              <span className="scenario-number" aria-hidden="true">01</span>
              <span className="scenario-status scenario-status--available">사용 가능</span>
            </div>
            <p className="eyebrow">시나리오 1</p>
            <h2>회원가입 기능 자동 개발·검증</h2>
            <p className="scenario-description">
              Planner, Developer, QA, Security Agent가 협업하여
              회원가입 기능을 개발하고 검증하는 과정을 실행합니다.
            </p>
            <div className="scenario-tags" aria-label="참여 Agent">
              <span>Planner</span><span>Developer</span><span>QA</span><span>Security</span>
            </div>
            <div className="scenario-card-footer">
              <p>실행 화면에서 직접 Run을 시작할 수 있습니다.</p>
              {/* 화면만 이동하며, 여기서는 Orchestrator API를 호출하지 않습니다. */}
              <button type="button" className="app-button app-button--primary" onClick={onRunScenario}>
                시나리오 실행 <span aria-hidden="true">→</span>
              </button>
            </div>
          </article>

          <article className="scenario-card scenario-card--upcoming">
            <div className="scenario-card-heading">
              <span className="scenario-number" aria-hidden="true">02</span>
              <span className="scenario-status">준비 중</span>
            </div>
            <p className="eyebrow">시나리오 2</p>
            <h2>전자문서 제출·조회</h2>
            <p className="scenario-description">
              전자문서 업로드, 목록 조회, 다운로드 및 권한 기능을
              Agent 협업으로 구현하고 검증하는 시나리오입니다.
            </p>
            <div className="scenario-tags" aria-label="예정된 기능">
              <span>업로드</span><span>조회</span><span>다운로드</span><span>권한 관리</span>
            </div>
            <div className="scenario-card-footer">
              <p>Agent Pipeline 연결을 준비하고 있습니다.</p>
              <button type="button" className="app-button" disabled>준비 중</button>
            </div>
          </article>
        </section>
      </div>
    </main>
  )
}

export default DashboardPage
