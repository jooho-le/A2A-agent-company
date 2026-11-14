// 책상·의자·소품·착석 위치를 같은 좌표계로 옮기는 표시용 오피스 배치입니다.
// Agent의 역할, 실행 순서와 실제 API 설정에는 영향을 주지 않습니다.
import { roundedTeamPath } from './office3dTeamRun.js'

const radians = (degrees) => degrees * Math.PI / 180
// 앞을 바라보는 방향과 가로 열을 분리해, 기존 책상을 교실처럼 정렬합니다.
// 오케스트레이터가 앞쪽에서 네 작업 에이전트와 마주 봅니다.
const studentYaw = radians(-54)
// 배경 사진에는 별도의 충돌 메시가 없습니다. 선반·소파·전경 난간을 제외한
// 빈 바닥에 다섯 좌석 전체가 들어오도록 작업 공간을 한꺼번에 맞춥니다.
// 좌석 사이 간격·가구 비율·합류 경로는 같은 로컬 좌표로 보존합니다.
export const officeFloorFit = { scale: 1.42, position: [-.7, 0, 2] }
function classroomPoint(column, row) {
  return {
    x: column * Math.cos(studentYaw) + row * Math.sin(studentYaw),
    z: -column * Math.sin(studentYaw) + row * Math.cos(studentYaw),
  }
}
function classroomDesk(column, row) {
  const { x, z } = classroomPoint(column, row)
  return [x, 0, z]
}
export const officeSeating = [
  { role: 'planner', desk: classroomDesk(-3.3, -1), yaw: studentYaw, exitSide: 1 },
  { role: 'developer', desk: classroomDesk(3.3, -1), yaw: studentYaw, exitSide: -1 },
  { role: 'qa', desk: classroomDesk(-3.3, -5.4), yaw: studentYaw, exitSide: 1, via: [classroomPoint(-.75, -2.6), classroomPoint(-.75, .9)] },
  { role: 'security', desk: classroomDesk(3.3, -5.4), yaw: studentYaw, exitSide: -1, via: [classroomPoint(.75, -2.6), classroomPoint(.75, .9)] },
  { role: 'orchestrator', desk: classroomDesk(0, 6.3), yaw: studentYaw + Math.PI, exitSide: -1 },
]

const reunion = { ...classroomPoint(0, 2.55), radius: 2 }
const slots = [-72, 72, -144, 144, 0].map(angle => radians(angle) + studentYaw)
const departures = [0, .35, .8, 1.15, .5]
const travelTimes = [6, 6, 7.1, 7.1, 6.3]

const clamp = (n) => Math.max(0, Math.min(1, n))
const smooth = (a, b, value) => {
  const t = clamp((value - a) / (b - a))
  return t * t * (3 - 2 * t)
}
const lerp = (a, b, t) => a + (b - a) * t
const angleMix = (a, b, t) => a + Math.atan2(Math.sin(b - a), Math.cos(b - a)) * t

export function seatingPoint(index, x, z) {
  const { desk, yaw } = officeSeating[index]
  return { x: desk[0] + x * Math.cos(yaw) + z * Math.sin(yaw), z: desk[2] - x * Math.sin(yaw) + z * Math.cos(yaw) }
}

export function applyOfficeSeating(office, engine) {
  const { P, En, er } = engine
  office.workspace.scale.setScalar(officeFloorFit.scale)
  office.workspace.position.set(...officeFloorFit.position)
  office.agents.forEach((agent, index) => {
    const { desk: target, yaw } = officeSeating[index]
    const desk = office.workspace.getObjectByName(`desk_${index}`)
    const oldDesk = desk.position.clone()
    const prop = office.props[index]
    const relativeProp = prop.position.clone().sub(oldDesk).applyAxisAngle(new P(0, 1, 0), yaw)
    desk.position.set(...target)
    desk.rotation.y = yaw
    prop.position.copy(desk.position).add(relativeProp)
    prop.rotation.y += yaw
    const seat = seatingPoint(index, 0, -.9)
    agent.seat.set(seat.x, 0, seat.z)
    agent.seatYaw = yaw
    agent.g.position.copy(agent.seat)
    agent.g.rotation.y = yaw
    office.positions[index] = [...target]
    if (index === 2) {
      office.cupOnDesk.sub(oldDesk).applyAxisAngle(new P(0, 1, 0), yaw).add(desk.position)
      office.cup.position.copy(office.cupOnDesk)
      office.cup.rotation.y = yaw
    }
  })
  // 총괄의 의자와 뒷줄 이동 통로에 장식 가구가 겹치지 않게 옮깁니다.
  office.workspace.getObjectByName('shared_planter').position.set(.5, 0, -5.8)
  office.workspace.getObjectByName('shared_sideboard_-1').position.set(-6.8, 0, -.4)
  office.workspace.getObjectByName('shared_sideboard_1').position.set(6.8, 0, -.4)
  // 보안 에이전트 몸통과 겹치는 오른쪽 수납장 전체를 숨깁니다.
  // 장면에는 남겨 두어 해제 시 메시와 재질이 기존 방식으로 정리되게 합니다.
  office.workspace.getObjectByName('shared_sideboard_1').visible = false
  office.confetti.groupRoot.position.set(office.workspace.position.x + reunion.x * office.workspace.scale.x, 0,
    office.workspace.position.z + (reunion.z - 4.58) * office.workspace.scale.z)

  office.seating = createOfficeRoutes({ En, er })
  office.root.updateMatrixWorld(true)
}

// 장면 객체와 분리해 이동 경로·충돌 간격을 자동 테스트할 수 있게 합니다.
export function createOfficeRoutes({ En, er, teamPaces = [2.25, .75, 2.35, .77, 2.45] }) {
  const teamPaths = officeSeating.map((seat, index) => {
    const [x, z] = [[-1.4, 2], [-4, 2.4], [1.6, -.4], [3.4, -1], [0, .8]][index]
    return roundedTeamPath([
      seatingPoint(index, 0, -1.45),
      seatingPoint(index, seat.exitSide * 2.55, -1.45),
      seatingPoint(index, seat.exitSide * 2.55, 1.65),
      ...(index >= 2 && index <= 3 ? [classroomPoint(index === 2 ? -.75 : .75, -2.6)] : []),
      ...(index === 4 ? [{ x: .7, z: 2.8 }] : []),
      { x, z },
    ])
  })
  // 모든 경로는 책상 뒤에서 일어난 뒤 옆 끝을 돌아 안쪽 통로로 들어옵니다.
  // 임의의 전역 x/z를 이동시키지 않아 회전한 책상을 가로지르지 않습니다.
  function route(index, elapsed, end, target, extra = [], pace = 1, via = officeSeating[index].via || []) {
    const standing = smooth(0, 1, elapsed)
    const side = officeSeating[index].exitSide
    if (elapsed <= 1) return { ...seatingPoint(index, 0, -.9 - .55 * standing), standing }
    const points = [
      seatingPoint(index, 0, -1.45),
      seatingPoint(index, side * 2.55, -1.45),
      seatingPoint(index, side * 2.55, 1.65),
      ...via,
      ...extra,
      target,
    ]
    const lengths = points.slice(1).map((point, i) => Math.hypot(point.x - points[i].x, point.z - points[i].z))
    let remaining = clamp((elapsed - 1) / (end - 1)) ** pace * lengths.reduce((sum, length) => sum + length, 0)
    for (let i = 0; i < lengths.length; i++) {
      if (remaining <= lengths[i] || i === lengths.length - 1) {
        const progress = smooth(0, 1, remaining / Math.max(.00001, lengths[i]))
        return { x: lerp(points[i].x, points[i + 1].x, progress), z: lerp(points[i].z, points[i + 1].z, progress), standing }
      }
      remaining -= lengths[i]
    }
  }

  function hugRoute(index, elapsed) {
    const local = Math.max(0, elapsed - departures[index]), end = travelTimes[index]
    const target = { x: reunion.x + Math.sin(slots[index]) * reunion.radius, z: reunion.z + Math.cos(slots[index]) * reunion.radius }
    return { ...route(index, local, end, target), local, end }
  }

  function teamRoute(index, elapsed) {
    const end = 7.3
    const path = teamPaths[index], pace = teamPaces[index]
    const u = clamp((elapsed - 1) / (end - 1)), eased = u * u * (3 - 2 * u)
    const distance = path.length * eased ** pace
    const speed = u > 0 && u < 1 ? path.length * pace * eased ** (pace - 1) * 6 * u * (1 - u) / (end - 1) : 0
    const standing = smooth(0, 1, elapsed)
    const pose = elapsed <= 1 ? seatingPoint(index, 0, -.9 - .55 * standing) : path.at(distance)
    const before = path.at(distance - .15), after = path.at(distance + .15)
    const movingYaw = Math.atan2(after.x - before.x, after.z - before.z)
    const leaving = smooth(.9, 1.35, elapsed), arriving = smooth(end - .55, end, elapsed)
    return { ...pose, elapsed, end, standing, distance, speed,
      yaw: angleMix(angleMix(officeSeating[index].yaw, movingYaw, leaving), er, arriving) }
  }

  function placeTeam(agent, time) {
    const pose = teamRoute(agent.i, Math.max(0, time - En))
    agent.g.position.x = pose.x
    agent.g.position.z = pose.z
    agent.g.rotation.y = pose.yaw
  }

  return { reunion, slots, hugRoute, placeTeam, teamRoute }
}
