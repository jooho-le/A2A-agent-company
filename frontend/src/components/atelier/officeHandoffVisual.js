// 실제 수신자가 확인된 문서의 표시만 담당합니다. 이벤트 순서와 시간은 Trace 제어기가 관리합니다.
export function createOfficeHandoff(workspace, name, origin, target, engine, lane = 0) {
  const paper = engine.Bx(workspace, name)
  const materials = []
  let part = 0
  paper.traverse(mesh => {
    if (!mesh.isMesh) return
    const material = new engine.OfficeBasicMaterial({
      color: part === 0 ? '#c8ee64' : part === 1 ? '#fffdf2' : '#33455a',
      transparent: true, opacity: 0, toneMapped: false, fog: false,
    })
    part++
    materials.push(material)
    // Bx의 공용 geometry/material은 수정하거나 해제하지 않습니다.
    mesh.material = material
    mesh.castShadow = false
  })
  const height = 2.4 + lane * .5
  const pointAt = t => {
    const point = origin.clone().lerp(target, t)
    point.y += Math.sin(Math.PI * t) * height
    return point
  }
  const points = Array.from({ length: 25 }, (_, i) => pointAt(i / 24).toArray())
  const route = engine.Xo(workspace, '#dceaa5', points, .025)
  const routeMaterial = new engine.OfficeBasicMaterial({ color: '#dceaa5',
    transparent: true, opacity: 0, depthWrite: false, toneMapped: false, fog: false })
  route.material = routeMaterial
  route.castShadow = false
  route.name = `${name}_route`
  materials.push(routeMaterial)
  const cameraRotation = paper.quaternion.clone(), parentRotation = paper.quaternion.clone()
  paper.scale.setScalar(1.5)
  return {
    paper,
    update(progress, camera) {
      const t = progress * progress * (3 - 2 * progress)
      const fade = Math.min(1, progress / .08, (1 - progress) / .12)
      paper.position.copy(pointAt(t))
      // 종이의 법선(+Y)을 카메라 쪽으로 향하게 해 얇은 옆면만 보이지 않도록 합니다.
      camera.getWorldQuaternion(cameraRotation)
      workspace.getWorldQuaternion(parentRotation)
      paper.quaternion.copy(parentRotation.invert()).multiply(cameraRotation)
      paper.rotateX(Math.PI / 2)
      paper.rotateY(Math.sin(Math.PI * t) * .12)
      materials.forEach(material => { material.opacity = Math.max(0, fade) })
      routeMaterial.opacity *= .32
      route.visible = paper.visible
    },
    dispose() {
      workspace.remove(paper, route)
      materials.forEach(material => material.dispose())
      route.geometry.dispose()
    },
  }
}
