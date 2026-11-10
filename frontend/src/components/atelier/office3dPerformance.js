// 기존 목업 연출과 역할별 오류 표정을 제공합니다. 가구와 서버의 실행 상태는 변경하지 않습니다.
import { applyTeamRun } from './office3dTeamRun.js'

export const finaleOptions = [
  { value: 'team', label: '1. 기존 협업 포즈' },
  { value: 'cheer', label: '2. 서류 던지기 · 박수' },
  { value: 'hug', label: '3. 달려와 포옹' },
]

const clamp = (value) => Math.max(0, Math.min(1, value))
const smooth = (start, end, value) => {
  const t = clamp((value - start) / (end - start))
  return t * t * (3 - 2 * t)
}
const mix = (a, b, t) => a + (b - a) * t

export function createOfficePerformance(office, engine) {
  const { P, Nt, ht, re, Xo, Bx, et, En, vs, At, pt } = engine
  const down = new P(0, -1, 0)
  const direction = new P(), bend = new P(), elbow = new P(), forearm = new P()
  const shoulderRotation = new Nt(), forearmRotation = new Nt()
  const center = new P()

  // 수납장 아래 빈틈에는 고정 받침을 두어 바닥 위에 떠 보이지 않게 합니다.
  for (let index = 0; index < office.agents.length; index++) {
    const storage = office.workspace.getObjectByName(`flush_storage_${index}`)
    if (!storage) continue
    for (const x of [-.24,.24]) for (const z of [-.36,.36]) {
      const foot = re(storage, '#444a4f', x, .0275, z, .065, .055, .065)
      foot.name = `storage_foot_${index}_${x}_${z}`
    }
  }

  // 두 관절의 길이는 기존 리그 값을 유지하고 손의 목표 위치만 바꿉니다.
  function reach(agent, side, target, amount = 1) {
    const arm = agent.arms[side]
    direction.set(...target).sub(arm.shoulder.position)
    const distance = Math.max(.015, Math.min(.835, direction.length()))
    direction.normalize()
    const along = (.44 ** 2 - .4 ** 2 + distance ** 2) / (2 * distance)
    const height = Math.sqrt(Math.max(0, .44 ** 2 - along ** 2))
    bend.set(side === 0 ? -1 : 1, -.35, -.12)
    bend.addScaledVector(direction, -bend.dot(direction)).normalize()
    elbow.copy(direction).multiplyScalar(along).addScaledVector(bend, height)
    forearm.copy(direction).multiplyScalar(distance).sub(elbow).normalize()
    shoulderRotation.setFromUnitVectors(down, elbow.normalize())
    forearmRotation.setFromUnitVectors(down, forearm)
    forearmRotation.premultiply(shoulderRotation.clone().invert())
    arm.shoulder.quaternion.slerp(shoulderRotation, amount)
    arm.elbow.quaternion.slerp(forearmRotation, amount)
  }

  function stand(agent, amount) {
    agent.upper.position.set(0, mix(1.36, 1.54, amount), mix(-.51, -.03, amount))
    agent.lower.position.set(0, 0, mix(-.25, 0, amount))
    agent.lower.scale.set(1, mix(1, 1.16, amount), 1)
    agent.upper.rotation.set(mix(.09, 0, amount), 0, 0)
    agent.head.rotation.set(0, 0, 0)
    agent.headphones.scale.setScalar(.0001)
    for (const leg of agent.legs) {
      leg.thigh.rotation.set(mix(-1.25, -.035, amount), 0, 0)
      leg.shin.rotation.set(mix(1.34, .08, amount), 0, 0)
    }
    agent.chair.position.set(0, 0, -1.35 - .4 * amount)
  }

  const papers = office.agents.map((agent, index) => {
    const paper = Bx(office.workspace, `celebration_document_${index}`)
    paper.scale.setScalar(.65)
    paper.visible = false
    paper.userData.mock = true
    return paper
  })

  function makeExpression(agent) {
    const original = agent.head.children.filter((object) => {
      if (!object.isMesh) return false
      object.geometry.computeBoundingBox()
      object.updateMatrix()
      object.geometry.boundingBox.getCenter(center).applyMatrix4(object.matrix)
      return center.z > .43 && ((center.y < -.19 && center.y > -.32 && Math.abs(center.x) < .06) || (center.y > .18 && center.y < .3 && Math.abs(center.x) > .12 && Math.abs(center.x) < .35))
    })
    const groups = {}
    for (const kind of ['worried', 'angry', 'sweat']) {
      const group = et(agent.head, `expression_${kind}`)
      const left = kind === 'angry' ? [[-.36,.27,.57],[-.24,.20,.58],[-.14,.17,.57]] : [[-.36,.21,.57],[-.24,.28,.58],[-.14,.30,.57]]
      const right = left.map(([x,y,z]) => [-x,y,z]).reverse()
      Xo(group, agent.cfg.hair, left, .025)
      Xo(group, agent.cfg.hair, right, .025)
      if (kind === 'angry') {
        re(group, '#79392f', 0, -.23, .51, .22, .065, .025)
        re(group, '#fff1dd', 0, -.22, .53, .18, .025, .009)
      } else if (kind === 'worried') {
        Xo(group, '#764534', [[-.13,-.26,.52],[-.055,-.22,.54],[.02,-.25,.54],[.12,-.21,.52]], .019)
      } else {
        Xo(group, '#764534', [[-.12,-.25,.52],[0,-.22,.55],[.12,-.25,.52]], .017)
      }
      const drops = []
      if (kind === 'sweat') {
        for (const [x,y] of [[.48,.25],[.55,.06],[-.49,.17]]) {
          drops.push(ht(group, '#8bd7ef', x, y, .42, .035, .075, .025))
        }
      }
      group.visible = false
      groups[kind] = { group, drops }
    }
    return { original, groups }
  }
  const expressions = office.agents.map(makeExpression)

  function setExpression(index, expression, local) {
    const agent = office.agents[index]
    const face = expressions[index]
    face.original.forEach((part) => { part.visible = true })
    Object.values(face.groups).forEach(({ group }) => { group.visible = false })
    agent.eyes.scale.x = 1
    agent.eyes.rotation.z = 0
    agent.g.userData.expression = 'normal'
    if (expression === 'normal') return
    const { group, drops } = face.groups[expression]
    face.original.forEach((part) => { part.visible = false })
    group.visible = true
    agent.eyes.scale.y = expression === 'angry' ? .66 : expression === 'sweat' ? .86 : 1.15
    agent.head.rotation.z += expression === 'worried' ? .10 : expression === 'angry' ? -.035 : .06
    agent.head.rotation.x = expression === 'angry' ? -.08 : -.04
    agent.head.rotation.y += .25
    if (expression === 'sweat') drops.forEach((drop, n) => {
      drop.position.y = [.25,.06,.17][n] - ((local * .35 + n * .2) % .3)
    })
    agent.g.userData.expression = expression
  }

  function errorFace(time, bubble) {
    let expression = 'normal'
    for (let index = 0; index < office.agents.length; index++) {
      const local = time - (At[index]?.start ?? 0) - 2 * pt
      const kind = bubble?.tone === 'error' && bubble.agent === index
        ? ['worried', 'angry', 'sweat'][Math.min(2, Math.max(0, Math.floor(local / 1.8)))] : 'normal'
      setExpression(index, kind, local)
      if (kind !== 'normal') expression = kind
    }
    return expression
  }

  function clapFinale(elapsed) {
    office.agents.forEach((agent, index) => {
      const local = Math.max(0, elapsed - index * .12)
      const standing = smooth(0, 1.25, local)
      agent.g.position.copy(agent.seat)
      const yaw = agent.seatYaw || 0
      agent.g.position.x -= Math.sin(yaw) * .28 * standing
      agent.g.position.z -= Math.cos(yaw) * .28 * standing
      agent.g.rotation.set(0, yaw + .12 * Math.sign(-agent.seat.x) * standing, 0)
      stand(agent, standing)
      const throwing = smooth(.6, 1.2, local) * (1 - smooth(1.7, 2.6, local))
      const clapping = smooth(2, 2.8, local)
      const settling = 1 - smooth(9.6, 11, local)
      const gap = .04 + .25 * Math.abs(Math.sin(local * 5.4)) * settling
      for (let side = 0; side < 2; side++) {
        const sign = side === 0 ? -1 : 1
        reach(agent, side, [sign * .28, -.2, .3], standing)
        reach(agent, side, [sign * .69, 1.15, .28], throwing)
        reach(agent, side, [sign * gap, .68, .55], clapping)
      }
      agent.upper.rotation.z = Math.sin(local * 4) * .025 * clapping * settling
      const paper = papers[index], flight = local - 1.05
      paper.visible = flight >= 0
      if (paper.visible) {
        const height = 2.5 + 3.7 * flight - 1.75 * flight ** 2
        // 던진 서류가 책상을 뚫고 바닥으로 내려가지 않도록 상판 위에 놓습니다.
        const airborne = height > 1.56
        const age = Math.min(flight, 2.35)
        const x = .3 + age * .2, z = .2 + age * .34
        paper.position.set(agent.seat.x + x * Math.cos(yaw) + z * Math.sin(yaw), Math.max(1.56, height), agent.seat.z - x * Math.sin(yaw) + z * Math.cos(yaw))
        paper.rotation.set(airborne ? age * 3 : 0, yaw + age * 2, airborne ? age * 1.3 : 0)
      }
    })
  }

  function hugFinale(elapsed) {
    const reunion = office.seating?.reunion || { x: 2, z: 5, radius: 1.5 }
    const slots = office.seating?.slots || [Math.PI * 1.25, Math.PI * .75, Math.PI * 1.75, Math.PI * .25]
    const departures = [2.8, 1.4, 1.3, 0]
    const turn = smooth(9.3, 11.7, elapsed) * Math.PI * .4
    const linking = smooth(8.9, 9.3, elapsed)
    const circleMotion = smooth(9.3, 9.6, elapsed) * (1 - smooth(11.3, 11.7, elapsed))
    const around = slots.map((angle, index) => ({ angle: (angle + Math.PI * 2) % (Math.PI * 2), index })).sort((a, b) => a.angle - b.angle)
    const next = [], previous = []
    around.forEach(({ index }, place) => {
      next[index] = around[(place + 1) % around.length].index
      previous[index] = around[(place - 1 + around.length) % around.length].index
    })

    // 새 자리 배치가 있으면 책상 옆 통로와 역할별 출발 시간을 사용합니다.
    // 배치 정보가 없는 기존 장면에서는 원래의 합류 경로를 유지합니다.
    function route(index, atTime) {
      if (office.seating) return office.seating.hugRoute(index, atTime)
      const agent = office.agents[index]
      const local = Math.max(0, atTime - departures[index])
      const standing = smooth(0, 1, local)
      const end = index < 2 ? 6.4 : 5.4
      const exit = smooth(1, 2.2, local)
      const running = smooth(2.2, end, local)
      const targetX = reunion.x + Math.sin(slots[index]) * reunion.radius
      const targetZ = reunion.z + Math.cos(slots[index]) * reunion.radius
      const z = mix(agent.seat.z - .35 * standing, targetZ, running)
      const right = agent.seat.x > 0
      const fan = smooth(3.35, 3.9, z)
      const x = mix(mix(agent.seat.x, right ? .6 : -.6, exit), targetX, fan)
      return { x, z, local, standing, end }
    }

    const poses = office.agents.map((agent, index) => {
      const pose = route(index, elapsed)
      const angle = slots[index] + turn
      if (elapsed >= 9.3) {
        pose.x = reunion.x + Math.sin(angle) * reunion.radius
        pose.z = reunion.z + Math.cos(angle) * reunion.radius
      }
      return pose
    })

    office.agents.forEach((agent, index) => {
      const pose = poses[index]
      const { local, standing, end } = pose
      const before = route(index, elapsed - .22), after = route(index, elapsed + .22)
      const movement = Math.hypot(after.x - before.x, after.z - before.z)
      const towardCenter = Math.atan2(reunion.x - pose.x, reunion.z - pose.z)
      const movingYaw = movement > .00001 ? Math.atan2(after.x - before.x, after.z - before.z) : towardCenter
      const facing = smooth(end - .8, end + .1, local)
      const angleDifference = Math.atan2(Math.sin(towardCenter - movingYaw), Math.cos(towardCenter - movingYaw))
      const seatYaw = agent.seatYaw || 0
      const direction = movingYaw + angleDifference * facing
      const yaw = elapsed >= 9.3 ? towardCenter : seatYaw + Math.atan2(Math.sin(direction - seatYaw), Math.cos(direction - seatYaw)) * smooth(.9, 1.5, local)
      agent.g.rotation.set(0, yaw, 0)
      stand(agent, standing)

      const running = smooth(1, 1.3, local) * (1 - smooth(end - .25, end, local))
      const gait = Math.sin(local * 9) * running
      const circleGait = Math.sin((elapsed - 9.3) * 8 + index * Math.PI * 2 / office.agents.length) * circleMotion
      const bounce = elapsed >= 9.3 ? Math.abs(Math.sin((elapsed - 9.3) * 8)) * .012 * circleMotion : Math.abs(gait) * .025
      agent.g.position.set(pose.x, bounce, pose.z)
      agent.legs.forEach((leg, side) => {
        const swing = gait * (side === 0 ? 1 : -1)
        leg.thigh.rotation.x += .33 * swing
        leg.shin.rotation.x += .4 * Math.max(0, -swing) + .12 * Math.abs(circleGait)
        leg.thigh.rotation.z += .12 * circleGait * (side === 0 ? 1 : -1)
      })

      const nearest = Math.min(...poses.filter((_, other) => other !== index).map((other) => Math.hypot(pose.x - other.x, pose.z - other.z)))
      // 가까운 동료를 지나갈 때는 잠시 팔을 낮춰 손과 몸이 겹치지 않게 합니다.
      const armsOpen = smooth(2.2, 3, local) * (1 - smooth(end - .9, end, local)) * smooth(1.95, 2.65, nearest)
      for (let side = 0; side < 2; side++) {
        const sign = side === 0 ? -1 : 1
        reach(agent, side, [sign * .28, -.2, .25], standing)
        reach(agent, side, [sign * 1.03, .85, .13], armsOpen)
        const neighbor = side === 0 ? next[index] : previous[index]
        const pair = Math.min(index, neighbor) * office.agents.length + Math.max(index, neighbor)
        const pat = Math.sin((elapsed - 9.3) * 3 + pair) * .025 * circleMotion
        // 같은 이웃 쌍은 같은 높이로 손을 움직이며 서로를 감싸는 원을 만듭니다.
        reach(agent, side, [sign * reunion.radius / 2, .56 + pat, reunion.radius / 2 + .03], linking)
      }
      agent.head.rotation.x = -.035 * facing
      agent.head.rotation.z = Math.sin(elapsed * 2 + index) * .012 * circleMotion
    })
  }

  function confetti(elapsed) {
    for (const {piece, x, y, z, delay, speed, size, spin} of office.confetti.pieces) {
      const age = elapsed - .3 - delay
      const cycle = Math.max(0, age) % 4.5
      const falling = y - cycle * speed - .23 * cycle ** 2
      const visible = age > 0 && elapsed < 10.7 && falling > .1
      piece.position.set(x + Math.sin(cycle * 2 + z) * .6, falling, z + Math.sin(cycle + x) * .5)
      piece.rotation.set(cycle * spin, cycle * spin * .6, cycle * spin * .8)
      piece.scale.set(visible ? size : 0, visible ? size * 1.72 : 0, visible ? 1 : 0)
    }
  }

  function update(time, finale, bubble, motionEnabled) {
    papers.forEach((paper) => { paper.visible = false })
    office.agents.forEach((agent) => { agent.lower.scale.set(1, 1, 1) })
    if (time >= En && finale === 'team' && office.seating?.teamRoute) {
      office.agents.forEach(agent => applyTeamRun(agent, office.seating.teamRoute(agent.i, time - En)))
    }
    if (time >= En && finale !== 'team') {
      office.book.scale.setScalar(.0001)
      office.cup.position.copy(office.cupOnDesk)
      office.cup.rotation.set(0, 0, 0)
      if (finale === 'cheer') clapFinale(time - En)
      else hugFinale(time - En)
      confetti(time - En)
    }
    office.confetti.groupRoot.visible = motionEnabled && time >= (finale === 'team' ? vs - .45 : En + .3)
    const expression = errorFace(time, bubble)
    office.root.updateMatrixWorld(true)
    return expression
  }

  // 실제 모드는 같은 오류 표정만 재사용하고 전체 협업 완료 연출은 호출하지 않습니다.
  function updateActual(agents) {
    papers.forEach(paper => { paper.visible = false })
    office.confetti.groupRoot.visible = false
    office.agents.forEach((agent, index) => {
      agent.lower.scale.set(1, 1, 1)
      setExpression(index, agents[index]?.expression ?? 'normal', 0)
    })
    office.root.updateMatrixWorld(true)
  }

  return { update, updateActual, traceExpression: (index, progress) => setExpression(index, 'worried', progress) }
}
