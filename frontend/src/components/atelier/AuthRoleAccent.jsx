// 로그인 장식 전용 SVG입니다. 실제 업무 상태나 결과를 나타내지 않습니다.
export default function AuthRoleAccent({ role }) {
  if (role === 'orchestrator') return null
  return <svg className={`auth-role-art auth-role-art--${role}`} viewBox="0 0 200 170" fill="none" aria-hidden="true">
    {role === 'planner' && <>
      <rect className="role-layer" x="23" y="8" width="164" height="133" rx="14" transform="rotate(7 105 75)" />
      <rect className="role-panel" x="9" y="21" width="164" height="136" rx="14" />
      <path className="role-muted" d="M24 36h37m-37 8h23" />
      <path className="role-wire" d="M91 74v17H44v17m47-17h48v17M44 130v12h95v-12" />
      <rect className="role-block" x="67" y="53" width="48" height="24" rx="6" />
      <rect className="role-block" x="24" y="107" width="41" height="25" rx="6" />
      <rect className="role-block" x="119" y="107" width="41" height="25" rx="6" />
      <path className="role-ink" d="m83 65 5 5 10-11m-66 61h16m79 0h17" />
      <circle className="role-dot role-pulse" cx="91" cy="91" r="4" />
      <path className="role-ink" d="m167 9 4-7m10 19 9-2" />
    </>}
    {role === 'developer' && <>
      <rect className="role-layer" x="24" y="8" width="166" height="120" rx="12" />
      <rect className="role-panel" x="5" y="23" width="169" height="124" rx="12" />
      <path className="role-divider" d="M6 46h167M34 47v87" />
      <g className="role-dot"><circle cx="19" cy="35" r="3" /><circle cx="29" cy="35" r="3" opacity=".5" /><circle cx="39" cy="35" r="3" opacity=".3" /></g>
      <path className="role-muted" d="M18 62h6m-6 17h6m-6 17h6m-6 17h6M48 62h24m-12 17h40m-40 17h21M48 113h29" />
      <path className="role-ink" d="M83 62h46M110 79h38M92 96h32" />
      <g transform="rotate(8 145 133)">
        <rect className="role-badge" x="105" y="111" width="89" height="48" rx="10" />
        <path className="role-ink" d="m127 125-10 10 10 10m43-20 10 10-10 10m-18-22-9 25" />
      </g>
      <path className="role-cursor" d="M86 109v9" />
    </>}
    {role === 'qa' && <>
      <rect className="role-layer" x="21" y="15" width="161" height="126" rx="13" />
      <rect className="role-panel" x="9" y="28" width="160" height="123" rx="13" />
      <path className="role-divider" d="M10 51h157" />
      <path className="role-muted" d="M23 40h37M26 65h53v40H26zM26 117h47m-47 12h34" />
      <path className="role-ink" d="m30 95 12-14 10 7 12-14 10 10" />
      <circle className="role-badge" cx="132" cy="85" r="39" />
      <circle className="role-scan" cx="132" cy="85" r="30" />
      <path className="role-ink" d="m117 85 10 10 21-23" />
      <path className="role-muted" d="m158 115 25 27" />
      <g className="role-dot"><circle cx="91" cy="132" r="3" /><circle cx="103" cy="132" r="3" /><circle cx="115" cy="132" r="3" /></g>
    </>}
    {role === 'security' && <>
      <path className="role-wire" d="M36 49h29m70 0h29v80h-29M36 49v80h29" />
      <rect className="role-layer" x="58" y="14" width="99" height="140" rx="16" transform="rotate(7 108 84)" />
      <rect className="role-panel" x="47" y="20" width="99" height="139" rx="15" />
      <path className="role-muted" d="M65 37h32m-32 103h39" />
      <path className="role-ink role-fingerprint" d="M71 99V81a25 25 0 0 1 50 0v18m-42 11V81a17 17 0 0 1 34 0v29M87 119V81a9 9 0 0 1 18 0v40M96 81v40M64 103v-9m64 14v-14" />
      <rect className="role-badge" x="13" y="33" width="33" height="33" rx="9" />
      <rect className="role-badge" x="151" y="113" width="35" height="35" rx="9" />
      <path className="role-ink" d="m21 49 5 5 11-12m125 88 5 5 11-12" />
      <circle className="role-dot" cx="164" cy="49" r="4" />
    </>}
  </svg>
}
