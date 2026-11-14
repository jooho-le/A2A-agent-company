// Offline portrait scene. Reuses the office meshes, clothing, badges and hair;
// only these isolated instances receive the login illustration poses and props.
import * as engine from '../src/components/atelier/office3dScene'
import { addOfficeOrchestrator, updateOfficeBadges } from '../src/components/atelier/office3dOrchestrator'
import { tidyOfficeHair } from '../src/components/atelier/office3dAppearance'
import { humanAgents } from '../src/components/atelier/humanAgents'

const { P, Nt, et, re, ht, Xo } = engine
const down = new P(0, -1, 0)

function arm(agent, side, upper, forearm) {
  const joint = agent.arms[side]
  joint.shoulder.quaternion.setFromUnitVectors(down, new P(...upper).normalize())
  const target = new Nt().setFromUnitVectors(down, new P(...forearm).normalize())
  joint.elbow.quaternion.copy(joint.shoulder.quaternion).invert().multiply(target)
}

function handPosition(agent, side) {
  agent.g.updateWorldMatrix(true, true)
  return agent.g.worldToLocal(agent.arms[side].hand.getWorldPosition(new P()))
}

function tablet(parent, position, color = '#6c7f94') {
  const group = et(parent, 'login_tablet', position)
  re(group, color, 0, 0, 0, .51, .68, .055, { metalness: .3, roughness: .35 })
  re(group, '#223342', 0, .015, .033, .45, .58, .012)
  for (let i = 0; i < 3; i++) re(group, i === 0 ? '#d4ff64' : '#8da7b7', -.02, .16 - i * .13, .043, .31, .025, .009)
  return group
}

function posePlanner(agent) {
  agent.g.rotation.y = -.08
  agent.head.rotation.set(.17, .04, -.04)
  agent.upper.rotation.set(.015, 0, 0)
  // Keep both elbows outside the chart and wrap each hand around its side.
  arm(agent, 0, [-.45, -.65, .6], [.25, .15, .96])
  arm(agent, 1, [.45, -.65, .6], [-.25, .15, .96])
  agent.legs[0].thigh.rotation.z = -.12
  agent.legs[1].thigh.rotation.set(-.16, 0, .13)
  agent.legs[1].shin.rotation.x = .22
  const paper = engine.Bx(agent.g, 'login_clipboard')
  paper.position.set(0, 1.63, .65)
  paper.rotation.set(1.4, 0, 0)
  paper.scale.set(1.2, 1, .9)
}

function poseDeveloper(agent) {
  agent.g.rotation.y = .52
  agent.upper.rotation.set(.13, -.06, -.04)
  agent.head.rotation.set(.19, -.12, .02)
  agent.headphones.scale.setScalar(1)
  agent.legs[0].thigh.rotation.set(-1.35, -.08, -.12)
  agent.legs[1].thigh.rotation.set(-1.1, .13, .14)
  agent.legs[0].shin.rotation.x = 1.4
  agent.legs[1].shin.rotation.x = 1.3
  arm(agent, 0, [.04, -.45, .9], [.1, -.5, 1])
  arm(agent, 1, [-.04, -.45, .9], [-.1, -.5, 1])
  const laptop = engine.createOfficeWorkProp(agent.g, 1, 0, 0)
  laptop.position.set(0, 1.3, .82)
  laptop.rotation.y = Math.PI
  re(agent.g, '#373548', 0, 1.05, -.02, .83, .14, .7)
  re(agent.g, '#494357', 0, 1.53, -.31, .81, .85, .13)
}

function poseQa(agent) {
  agent.g.rotation.y = -.38
  agent.upper.rotation.set(.08, -.1, -.06)
  agent.head.rotation.set(.02, -.18, .08)
  arm(agent, 1, [.8, -.25, .6], [-.1, .95, .2])
  arm(agent, 0, [-.4, -.7, .55], [.15, .25, .9])
  agent.legs[0].thigh.rotation.set(-.28, 0, -.13)
  agent.legs[0].shin.rotation.x = .4
  agent.legs[1].thigh.rotation.z = .17
  const hand = handPosition(agent, 1)
  const ring = hand.clone().add(new P(.06, .37, .1))
  Xo(agent.g, '#d4ee7c', Array.from({ length: 49 }, (_, i) => {
    const a = i / 48 * Math.PI * 2
    return [ring.x + .24 * Math.cos(a), ring.y + .24 * Math.sin(a), ring.z]
  }), .042)
  Xo(agent.g, '#647b4e', [hand.clone().add(new P(0, -.09, 0)).toArray(), hand.toArray(), ring.clone().add(new P(0, -.23, 0)).toArray()], .05)
  const glass = ht(agent.g, '#b8e2d6', ring.x, ring.y, ring.z, .215, .215, .014)
  glass.material = glass.material.clone()
  glass.material.transparent = true
  glass.material.opacity = .2
  tablet(agent.g, [-.35, 1.54, .61], '#829480').rotation.set(-.12, -.25, -.17)
}

function shield(parent, position) {
  const group = et(parent, 'login_shield', position)
  const outline = [[0, .57, 0], [.45, .4, 0], [.4, -.21, 0], [0, -.57, 0], [-.4, -.21, 0], [-.45, .4, 0]]
  const mesh = re(group, '#567963', 0, 0, 0, 1, 1, 1, { metalness: .38, roughness: .26 })
  const geometry = new engine.OfficePlaneGeometry(1, 1)
  const Attribute = geometry.getAttribute('position').constructor
  geometry.setAttribute('position', new Attribute(new Float32Array([[0, 0, 0], ...outline].flat()), 3))
  geometry.deleteAttribute('uv')
  geometry.deleteAttribute('normal')
  geometry.setIndex(outline.flatMap((_, i) => [0, i + 1, (i + 1) % 6 + 1]))
  geometry.computeVertexNormals()
  mesh.geometry = geometry
  mesh.material = mesh.material.clone()
  mesh.material.side = 2
  Xo(group, '#d8ff8c', [...outline, outline[0]], .026)
  re(group, '#d4ff65', 0, -.05, .04, .27, .27, .055)
  Xo(group, '#d4ff65', [[-.09, .08, .04], [-.09, .22, .04], [0, .28, .04], [.09, .22, .04], [.09, .08, .04]], .024)
  ht(group, '#34492f', 0, -.04, .078, .023, .044, .01)
  return group
}

function poseSecurity(agent) {
  agent.g.rotation.y = -.23
  agent.upper.rotation.set(.025, .13, .08)
  agent.head.rotation.set(-.04, -.1, -.07)
  agent.legs[0].thigh.rotation.set(-.2, 0, -.3)
  agent.legs[1].thigh.rotation.set(.1, 0, .27)
  agent.legs[0].shin.rotation.x = .27
  agent.legs[1].shin.rotation.x = .1
  arm(agent, 0, [-.7, -.6, .3], [0, .75, .65])
  arm(agent, 1, [.4, -.5, .8], [-.15, .5, .85])
  shield(agent.g, [.48, 1.63, .87]).rotation.set(-.06, -.18, -.12)
}

function poseOrchestrator(agent) {
  agent.g.rotation.y = -.18
  agent.upper.rotation.set(.025, -.04, -.025)
  agent.head.rotation.set(.48, -.1, -.035)
  // 팔꿈치는 몸 곁에 두고 왼쪽 팔뚝을 가로로 들어 손목 위쪽을 바라봅니다.
  const forearm = new P(.95, .08, .32).normalize()
  arm(agent, 0, [-.12, -.32, .72], forearm.toArray())
  // 반대 손은 아래에서 팔을 받쳐, 양 주먹을 든 자세로 보이지 않게 합니다.
  arm(agent, 1, [.2, -.85, .25], [-.85, .32, .42])
  agent.legs[0].thigh.rotation.set(-.08, -.04, -.13)
  agent.legs[0].shin.rotation.x = .08
  agent.legs[1].thigh.rotation.set(.12, .04, .16)
  agent.legs[1].shin.rotation.x = .15

  const wrist = handPosition(agent, 0).addScaledVector(forearm, -.14)
  agent.g.updateWorldMatrix(true, true)
  const eyes = agent.g.worldToLocal(agent.eyes.getWorldPosition(new P()))
  // 카메라가 아닌 실제 눈 위치를 향하는 손목의 윗면에 시계판을 붙입니다.
  const faceNormal = eyes.sub(wrist).normalize()
  faceNormal.addScaledVector(forearm, -faceNormal.dot(forearm)).normalize()
  const tangent = new P().crossVectors(forearm, faceNormal).normalize()
  Xo(agent.g, '#343a42', Array.from({ length: 33 }, (_, i) => {
    const angle = i / 32 * Math.PI * 2
    return wrist.clone().addScaledVector(faceNormal, Math.cos(angle) * .12)
      .addScaledVector(tangent, Math.sin(angle) * .12).toArray()
  }), .026)
  const watch = et(agent.g, 'login_leader_watch', wrist.clone().addScaledVector(faceNormal, .135).toArray())
  watch.quaternion.setFromUnitVectors(new P(0, 0, 1), faceNormal)
  watch.scale.setScalar(.82)
  ht(watch, '#b5bbc2', 0, 0, 0, .137, .137, .035)
  ht(watch, '#152331', 0, 0, .029, .111, .111, .012)
  for (const [x, y] of [[0, .085], [.085, 0], [0, -.085], [-.085, 0]]) {
    ht(watch, '#dce2e4', x, y, .041, .009, .009, .004)
  }
  Xo(watch, '#e6e9e8', [[-.048, .036, .044], [0, 0, .044], [.024, .066, .044]], .008)
  ht(watch, '#b5bbc2', .145, 0, 0, .023, .025, .023)
}

export function renderLoginCharacters(roles = humanAgents.map(agent => agent.id)) {
  const renderer = new engine.wi({ antialias: true, alpha: true, preserveDrawingBuffer: true })
  renderer.setSize(768, 960)
  renderer.setPixelRatio(1)
  renderer.setClearColor(0x000000, 0)
  renderer.toneMapping = engine.wc
  renderer.toneMappingExposure = 1.12
  const scene = new engine.kn()
  const office = engine.Yu()
  addOfficeOrchestrator(office, engine)
  tidyOfficeHair(office)
  updateOfficeBadges(office, engine)
  scene.add(office.lights)
  const lights = office.lights.children
  lights[1].position.set(-3, 5, 6)
  lights[1].castShadow = false
  lights[2].position.set(3, 4, -3)
  lights[2].color.set('#d4ec94')
  lights[2].intensity = 2
  const camera = new engine.Dt(32, 768 / 960, .1, 40)
  camera.position.set(.35, 2.9, 7.5)
  camera.lookAt(0, 1.66, .12)
  const poses = [posePlanner, poseDeveloper, poseQa, poseSecurity, poseOrchestrator]
  const images = {}
  office.agents.forEach((agent, index) => {
    if (!roles.includes(humanAgents[index].id)) return
    scene.add(agent.g)
    agent.g.position.set(0, 0, 0)
    agent.g.rotation.set(0, 0, 0)
    agent.upper.position.set(0, 1.36, 0)
    agent.lower.position.set(0, 0, 0)
    agent.headphones.scale.setScalar(.0001)
    agent.eyes.scale.setScalar(1)
    agent.legs.forEach(leg => { leg.thigh.rotation.set(0, 0, 0); leg.shin.rotation.set(.04, 0, 0) })
    agent.head.rotation.set(0, 0, 0)
    agent.upper.rotation.set(0, 0, 0)
    // The office's reading prop is not part of the login pose.
    const book = agent.upper.getObjectByName('planner_open_book')
    if (book) book.visible = false
    poses[index](agent)
    scene.updateMatrixWorld(true)
    renderer.render(scene, camera)
    images[humanAgents[index].id] = renderer.domElement.toDataURL('image/png')
    scene.remove(agent.g)
  })
  renderer.dispose()
  renderer.forceContextLoss()
  return images
}
