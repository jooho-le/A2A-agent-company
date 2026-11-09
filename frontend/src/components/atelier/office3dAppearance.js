// 얼굴의 눈·표정·관절은 보존하고 귀 주변의 덧붙인 머리 볼륨만 정리합니다.
export function tidyOfficeHair(office) {
  office.agents.forEach(agent => {
    const hair = agent.head.children.find(part => part.name.startsWith('styled_hair_'))
    if (hair) {
      for (const part of hair.children) {
        if (Math.abs(part.position.x) > .4 && part.position.y < .3) part.visible = false
      }
      const scalp = hair.children[0]
      const positions = scalp?.geometry?.getAttribute('position')
      if (positions) {
        for (let index = 0; index < positions.count; index++) {
          const x = positions.getX(index), y = positions.getY(index)
          if (Math.abs(x) > .52 && y < .2) {
            const taper = Math.min(1, (.2 - y) / .35)
            positions.setX(index, x * (1 - taper * .07))
          }
        }
        positions.needsUpdate = true
        scalp.geometry.computeVertexNormals()
        scalp.geometry.computeBoundingBox()
        scalp.geometry.computeBoundingSphere()
      }
    }
    // 귀는 작고 둥근 형태로 두고 머리 옆에 뾰족한 덩어리처럼 나오지 않게 합니다.
    for (const part of agent.head.children) {
      if (part.isMesh && Math.abs(Math.abs(part.position.x) - .57) < .001
        && Math.abs(part.position.y + .04) < .001 && Math.abs(part.position.z) < .001) {
        part.scale.set(.085, .135, .085)
      }
    }
  })
}

// 정수리부터 머리 끝까지 같은 격자를 사용합니다. 앞쪽 헤어라인만 비대칭으로
// 내려 깻잎 앞머리를 만들고, 옆·뒤는 얼굴 뒤에서 곧게 이어집니다.
export function orchestratorHairPoint(u, v) {
  const angle = u * Math.PI * 2
  const front = Math.cos(angle), side = Math.sin(angle)
  const fringeBottom = .18 + .23 * ((1 - side) / 2) ** 1.4
  const blend = Math.max(0, Math.min(1, front / .32))
  const rim = -1.02 + (fringeBottom + 1.02) * blend * blend * (3 - 2 * blend)
  const y = .72 + (rim - .72) * v
  const radius = y >= .06 ? Math.sqrt(Math.max(0, 1 - ((y - .06) / .66) ** 2))
    : 1 - .055 * Math.max(0, (.06 - y) / 1.08)
  return { x: .635 * radius * side, y, z: -.035 + .595 * radius * front }
}

export function addOrchestratorFringe(agent, engine) {
  const geometry = new engine.OfficePlaneGeometry(1, 1, 96, 40)
  const uv = geometry.getAttribute('uv'), positions = geometry.getAttribute('position')
  for (let index = 0; index < positions.count; index++) {
    const { x, y, z } = orchestratorHairPoint(uv.getX(index), uv.getY(index))
    positions.setXYZ(index, x, y, z)
  }
  geometry.computeVertexNormals()
  const scalp = agent.head.children.find(part => part.name.startsWith('styled_hair_'))
  const material = scalp.children[0].material.clone()
  material.color.set(agent.cfg.hair)
  material.side = 2 // 안쪽에서 보이는 긴 머리 끝도 같은 색으로 표시합니다.
  const fringe = new engine.OfficeMesh(geometry, material)
  fringe.name = 'orchestrator_side_swept_fringe'
  fringe.userData.style = '깻잎 앞머리'
  fringe.userData.continuousHair = true
  agent.head.add(fringe)
}
