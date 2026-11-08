// 책상·의자·소품·착석 위치를 같은 좌표계로 옮기는 표시용 오피스 배치입니다.
// Agent의 역할, 실행 순서와 실제 API 설정에는 영향을 주지 않습니다.
const radians = (degrees) => degrees * Math.PI / 180
// 앞을 바라보는 방향과 가로 열을 분리해, 기존 책상을 교실처럼 정렬합니다.
// 카메라에서는 총괄이 왼쪽 앞, 나머지는 오른쪽의 두 열로 보입니다.
const studentYaw = radians(-54)
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
  { role: 'planner', desk: classroomDesk(0, 4.8), yaw: studentYaw + Math.PI, exitSide: -1 },
  { role: 'developer', desk: classroomDesk(-3, -.7), yaw: studentYaw, exitSide: -1 },
  { role: 'qa', desk: classroomDesk(3, -.7), yaw: studentYaw, exitSide: 1 },
  { role: 'security', desk: classroomDesk(0, -4.9), yaw: studentYaw, exitSide: -1, via: [classroomPoint(0, -3.25), classroomPoint(0, .95)] },
]

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
  // 앞자리와 첫 번째 열 사이의 빈 공간에서 기존 마무리 동작을 이어갑니다.
  const reunion = { ...classroomPoint(0, 2), radius: 1.4 }
  const slots = [Math.PI * 1.25, Math.PI * .75, Math.PI * 1.75, Math.PI * .25].map((angle) => angle + officeSeating[0].yaw)
  const departures = [1, 0, 1.25, 1.5]
  const travelTimes = [6, 6, 7, 5.5]

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
  office.confetti.groupRoot.position.set(reunion.x * office.workspace.scale.x, 0, (reunion.z - 4.58) * office.workspace.scale.z)

  // 모든 경로는 책상 뒤에서 일어난 뒤 옆 끝을 돌아 안쪽 통로로 들어옵니다.
  // 임의의 전역 x/z를 이동시키지 않아 회전한 책상을 가로지르지 않습니다.
  function route(index, elapsed, end, target, extra = [], pace = 1) {
    const standing = smooth(0, 1, elapsed)
    const side = officeSeating[index].exitSide
    if (elapsed <= 1) return { ...seatingPoint(index, 0, -.9 - .55 * standing), standing }
    const points = [
      seatingPoint(index, 0, -1.45),
      seatingPoint(index, side * 2.55, -1.45),
      seatingPoint(index, side * 2.55, 1.65),
      ...(officeSeating[index].via || []),
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

  function placeTeam(agent, time) {
    const elapsed = Math.max(0, time - En), index = agent.i
    const side = index === 2 ? -1 : index === 3 ? 1 : 0
    const x = side * 1.62 + (index === 0 ? .4 : index === 1 ? -.56 : 0)
    const z = index === 1 ? 1.23 : -.32
    const center = classroomPoint(0, 1.85)
    const target = { x: center.x + x * Math.cos(er) + z * Math.sin(er), z: center.z - x * Math.sin(er) + z * Math.cos(er) }
    const end = index < 2 ? 6.15 : 4.98
    // 첫 포즈는 앞뒤 두 명이 교차하지 않도록 합류 방향과 이동 속도를 나눕니다.
    // 도착 시각과 기존 포즈·관절 동작은 유지합니다.
    const extra = index === 0 ? [classroomPoint(2.55, 1.4)] : index === 1 ? [classroomPoint(-2.5, 1.4)] : []
    const pace = [2.6, .6, .45, .45][index]
    const pose = route(index, elapsed, end, target, extra, pace)
    const before = route(index, elapsed - .02, end, target, extra, pace), after = route(index, elapsed + .02, end, target, extra, pace)
    const moving = Math.hypot(after.x - before.x, after.z - before.z) > .00001
    const movingYaw = moving ? Math.atan2(after.x - before.x, after.z - before.z) : er
    const leaving = smooth(.9, 1.35, elapsed), arriving = smooth(end - .55, end, elapsed)
    agent.g.position.x = pose.x
    agent.g.position.z = pose.z
    agent.g.rotation.y = angleMix(angleMix(agent.seatYaw, movingYaw, leaving), er, arriving)
  }

  office.seating = { reunion, slots, hugRoute, placeTeam }
  office.root.updateMatrixWorld(true)
}
