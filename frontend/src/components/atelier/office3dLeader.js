import { officeLabelTexture, orchestratorIndex } from './office3dOrchestrator'

// 리더 캐릭터에 리더 소품과 책상 명패를 둡니다. Planner의 역할은 계획입니다.
export function addPlannerLeadership(office, engine) {
  const { et, re, ht, OfficePlaneGeometry, OfficeBasicMaterial, OfficeMesh } = engine
  const planner = office.agents[orchestratorIndex]
  const suit = et(planner.upper, 'planner_leadership_accessories')
  suit.userData.presentationOnly = true
  re(suit, '#ece6d6', 0, .49, .263, .16, .26, .018)
  for (const side of [-1, 1]) {
    const lapel = re(suit, '#253a4e', side * .125, .47, .277, .12, .26, .024)
    lapel.rotation.z = side * .32
  }
  const knot = re(suit, '#304963', 0, .48, .293, .043, .043, .018)
  knot.rotation.z = Math.PI / 4
  re(suit, '#304963', 0, .365, .291, .048, .19, .018)
  const tip = re(suit, '#304963', 0, .269, .291, .033, .033, .018)
  tip.rotation.z = Math.PI / 4
  ht(suit, '#d4b76c', -.18, .43, .306, .027, .027, .012)
  const badge = planner.upper.getObjectByName(`role_badge_${orchestratorIndex}`)
  if (badge) badge.position.y = .10

  const desk = office.workspace.getObjectByName(`desk_${orchestratorIndex}`)
  // 카메라를 따라 돌리지 않고, 에이전트 앞쪽의 책상 중앙에 정방향으로 둡니다.
  const plate = et(desk, 'planner_leadership_nameplate', [0, 1.67, .8])
  plate.userData.label = '오케스트레이터 · 리더'
  re(plate, '#293b4c', 0, 0, 0, 1.12, .25, .05)
  re(plate, '#c1a76b', 0, -.11, .031, 1.12, .028, .016)
  // 내장 3D 글꼴은 일부 문자만 포함하므로 한글 시스템 글꼴을 텍스처로 사용합니다.
  // 별도 폰트 다운로드 없이 일반 UI에서 읽을 수 있는 한글을 그대로 표시합니다.
  const texture = officeLabelTexture('오케스트레이터', engine, '#f5dfaa')
  const geometry = new OfficePlaneGeometry(1.04, .235)
  const material = new OfficeBasicMaterial({ map: texture, transparent: true, toneMapped: false, depthWrite: false })
  // 앞뒤에 정방향으로 붙여, 자리나 카메라 각도가 바뀌어도 거꾸로 읽히지 않게 합니다.
  for (const side of [1, -1]) {
    const label = new OfficeMesh(geometry, material)
    label.name = side === 1 ? 'planner_leadership_plate_text' : 'planner_leadership_plate_text_back'
    label.userData.text = '오케스트레이터'
    label.position.set(0, .005, side * .03)
    label.rotation.y = side === 1 ? 0 : Math.PI
    plate.add(label)
  }
  planner.g.userData.presentationTitle = '오케스트레이터'
}
