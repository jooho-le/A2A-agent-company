import assert from 'node:assert/strict'
import test from 'node:test'
import { officeSeating, createOfficeRoutes } from '../src/components/atelier/office3dLayout.js'

test('관리 서버 리더석과 네 작업 에이전트는 서로 마주 본다', () => {
  assert.deepEqual(officeSeating.map(seat => seat.role), ['planner', 'developer', 'qa', 'security', 'orchestrator'])
  const leader = officeSeating[4]
  for (const worker of officeSeating.slice(0, 4)) {
    const dx = leader.desk[0] - worker.desk[0], dz = leader.desk[2] - worker.desk[2]
    assert.ok((Math.sin(worker.yaw) * dx + Math.cos(worker.yaw) * dz) / Math.hypot(dx, dz) > .85)
    assert.ok(Math.sin(leader.yaw) * -dx + Math.cos(leader.yaw) * -dz > 0)
  }
})
for (const kind of ['team', 'hug']) {
  test(`${kind} 다섯 명 합류 전 구간에서 몸통·얼굴 간격을 유지한다`, () => {
    const routes = createOfficeRoutes({ En: 0, er: .42 })
    for (let time = 0; time <= 12; time += .02) {
      const points = officeSeating.map((seat, index) => {
        if (kind === 'hug') return routes.hugRoute(index, time)
        const agent = { i: index, seatYaw: seat.yaw, g: { position: {}, rotation: {} } }
        routes.placeTeam(agent, time)
        return agent.g.position
      })
      for (let i = 0; i < 5; i++) for (let j = i + 1; j < 5; j++) {
        assert.ok(Math.hypot(points[i].x - points[j].x, points[i].z - points[j].z) > 1.45,
          `${kind} ${time.toFixed(2)}초: ${i}/${j} 겹침`)
      }
    }
  })
}
test('팀 최종 합류 위치는 책상과 떨어진 바닥에 둔다', () => {
  const routes = createOfficeRoutes({ En: 0, er: .42 })
  for (let index = 0; index < 5; index++) {
    const actor = { i: index, seatYaw: officeSeating[index].yaw, g: { position: {}, rotation: {} } }
    routes.placeTeam(actor, 12)
    for (const { desk, yaw } of officeSeating) {
      const dx = actor.g.position.x - desk[0], dz = actor.g.position.z - desk[2]
      const x = dx * Math.cos(yaw) - dz * Math.sin(yaw), z = dx * Math.sin(yaw) + dz * Math.cos(yaw)
      assert.ok(Math.hypot(Math.max(0, Math.abs(x) - 1.95), Math.max(0, Math.abs(z) - .95)) > .6)
    }
  }
})
