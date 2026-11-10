import { applyOfficeSeating } from './office3dLayout'
import { addPlannerLeadership } from './office3dLeader'
import { createAgentCloseupCamera } from './office3dPlannerCamera'
import { humanAgents } from './humanAgents'
import { createOfficePerformance, finaleOptions } from './office3dPerformance'
import { completeCelebration, getNextCelebration, resetCelebrationOrder } from './officeCelebrationSequence'
import { createOfficeRunController } from './office3dRunController'
import { createRunSuccessCeremony } from './runSuccessCeremony'
import { addOfficeOrchestrator, managementDisplay, updateOfficeBadges, orchestratorIndex } from './office3dOrchestrator'
import { createOfficeTraceController, isTraceActionCurrent } from './officeTraceEvents'
import { tidyOfficeHair } from './office3dAppearance'
import { buildOfficeRunPopups, popupResultNotice, renderOfficeRunPopups } from './officeRunPopups'

// 제공된 HTML의 시연·카메라 로직을 영역 내부로 한정한 수명 주기 어댑터입니다.
export function mountAgentOffice(root, {
  requestText,
  motionEnabled,
  displayMode = 'mock',
  runDisplayState = null,
  successCeremony = createRunSuccessCeremony()
}, engine) {
  const {
    wi,
    Tc,
    wc,
    kn,
    He,
    to,
    Dt,
    P,
    Do,
    Yu,
    Eo,
    ct,
    Ku,
    tr,
    al,
    bn,
    At,
    ci,
    Qt,
    En,
    pt,
    Zo,
    vs,
    Ms,
    getOfficeAgentPose,
    applyOfficeAgentPose
  } = engine;
  const events = new AbortController();
  let frame = 0;
  let disposed = false;
  let renderedOnce = false;
  const listen = (target, type, listener) => target.addEventListener(type, listener, {
    signal: events.signal
  });
  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(frame);
    events.abort();
    if (typeof resizeObserver !== 'undefined') resizeObserver.disconnect();
    controls?.dispose();
    const geometries = new Set(),
      materials = new Set(),
      textures = new Set();
    const disposeTree = tree => tree?.traverse(object => {
      if (object.geometry) geometries.add(object.geometry);
      for (const material of Array.isArray(object.material) ? object.material : [object.material]) {
        if (!material) continue;
        materials.add(material);
        for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
      }
      object.shadow?.map?.dispose();
    });
    disposeTree(office?.root);
    disposeTree(office?.room);
    textures.add(background);
    textures.forEach(texture => texture?.dispose());
    materials.forEach(material => material.dispose());
    geometries.forEach(geometry => geometry.dispose());
    renderer?.dispose();
    renderer?.forceContextLoss();
    renderer?.domElement.remove();
    agentLabels?.forEach(label => label.remove());
    liveRows?.forEach(row => row.remove());
  }
  try {
    var element = i => root.querySelector(`[data-office3d="${i}"]`),
      stage = element("stage"),
      renderer = new wi({
        antialias: !0,
        powerPreference: "high-performance"
      });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 1.75));
    renderer.shadowMap.enabled = !0;
    renderer.shadowMap.type = Tc;
    renderer.toneMapping = wc;
    renderer.toneMappingExposure = 1.06;
    stage.prepend(renderer.domElement);
    renderer.domElement.setAttribute("aria-label", "오케스트레이터와 네 에이전트가 협업하는 3D 사무실");
    var scene = new kn();
    scene.background = new He("#0b0e13");
    scene.fog = new to("#0b0e13", 35, 65);
    var camera = new Dt(39, 1, .1, 110),
      homeEye = new P(17, 15, 23),
      homeTarget = new P(-.35, 1.45, -.5);
    camera.position.copy(homeEye);
    var controls = new Do(camera, renderer.domElement);
    controls.target.copy(homeTarget);
    controls.enableDamping = !0;
    controls.dampingFactor = .07;
    controls.minDistance = 6;
    controls.maxDistance = 40;
    controls.maxPolarAngle = Math.PI * .48;
    controls.minPolarAngle = .25;
    controls.maxAzimuthAngle = Math.PI * .5;
    controls.minAzimuthAngle = -Math.PI * .5;
    controls.update();
    var office = Yu();
    addOfficeOrchestrator(office, engine);
    tidyOfficeHair(office);
    applyOfficeSeating(office, engine);
    updateOfficeBadges(office, engine);
    addPlannerLeadership(office, engine);
    scene.add(office.root);
    var acting = createOfficePerformance(office, engine);
    const workerOffice = { ...office, agents: office.agents.slice(0, orchestratorIndex) };
    // 원래 네 역할의 타임라인은 보존하고 관리 캐릭터만 별도로 표시합니다.
    function updateOfficePose(atTime) {
      Ms(workerOffice, atTime);
      const manager = office.agents[orchestratorIndex];
      applyOfficeAgentPose(manager, getOfficeAgentPose(0, atTime, atTime < En ? 'working' : 'waiting'));
      manager.g.position.copy(manager.seat);
      manager.g.rotation.set(0, manager.seatYaw, 0);
      manager.upper.position.set(0, 1.36, -.51); manager.lower.position.set(0, 0, -.25);
      manager.lower.scale.set(1, 1, 1);
      manager.legs.forEach(leg => { leg.thigh.rotation.set(-1.25, 0, 0); leg.shin.rotation.set(1.34, 0, 0); });
      manager.chair.position.set(0, 0, -1.35);
      if (atTime >= En) {
        engine.applyOfficeTeamPose(manager, getOfficeAgentPose(0, 0, 'waiting'), atTime);
        office.seating.placeTeam(manager, atTime);
      }
    }
    const runController = createOfficeRunController();
    const handoffs = createOfficeTraceController();
    handoffs.observe(runDisplayState, displayMode);
    const actualPapers = new Map();
    let traceUiKey = '';
    let actualPopupKey = '';
    let previousPopups = null;
    const fromPoint = new P(), toPoint = new P();
    function updateActualHandoffs(delta, enabled) {
      const actions = handoffs.advance(delta, enabled);
      const flights = actions.filter(item => item.from !== null || item.kind === 'artifact');
      const visible = new Set(flights.map(item => item.eventId));
      for (const [id, paper] of actualPapers) if (!visible.has(id)) {
        office.workspace.remove(paper); actualPapers.delete(id);
      }
      for (const item of flights) {
        let paper = actualPapers.get(item.eventId);
        if (!paper) { paper = engine.Bx(office.workspace, `actual_handoff_${item.eventId}`); actualPapers.set(item.eventId, paper); }
        paper.userData.eventId = item.eventId; paper.userData.from = item.from; paper.userData.to = item.to;
        const from = office.agents[item.from ?? item.agent], to = office.agents[item.to], t = item.progress;
        const amount = Math.sin(Math.PI * t);
        from.arms[1].shoulder.rotation.x -= amount * .45;
        from.arms[1].shoulder.rotation.z += amount * .35;
        office.root.updateMatrixWorld(true);
        from.arms[1].hand.getWorldPosition(fromPoint);
        office.workspace.worldToLocal(fromPoint);
        if (to) {
          to.arms[0].hand.getWorldPosition(toPoint); office.workspace.worldToLocal(toPoint);
          paper.position.copy(fromPoint).lerp(toPoint, t); paper.position.y += amount * 2;
        } else {
          // 산출물 등록은 해당 자리의 문서 표시이며, 확인되지 않은 다음 수신자를 만들지 않습니다.
          paper.position.copy(fromPoint); paper.position.y += .4 + amount * .6;
        }
        paper.scale.setScalar(.75); paper.rotation.set(t * 2, t * 3, Math.sin(Math.PI * t) * .4);
      }
      for (const item of actions) {
        if (item.agent === null || item.kind === 'log') continue;
        const model = office.agents[item.agent], pulse = Math.sin(Math.PI * item.progress);
        const selected = actualAgents[item.agent];
        const currentStep = isTraceActionCurrent(item, selected);
        // 과거 로그로 최신 Step의 작업·실패 상태를 덮어쓰지 않습니다.
        if (currentStep && selected?.displayState !== 'unknown') {
          if (['receive', 'state', 'verdict'].includes(item.kind)) model.head.rotation.x -= pulse * .1;
          if (item.kind === 'issue' && selected.displayState !== 'failed') acting.traceExpression(item.agent, item.progress);
        }
        if (item.agent === orchestratorIndex && ['receive', 'manage', 'verdict'].includes(item.kind)) {
          model.arms[0].shoulder.rotation.x -= pulse * .25;
          model.head.rotation.x -= pulse * .08;
        }
        for (const index of item.relatedAgents ?? []) {
          // 검증 작업 자체의 모션은 최신 Step RUNNING일 때만 기존 제어기가 재생합니다.
          if (actualAgents[index]?.displayState === 'working' && actualAgents[index].step?.codeVersion === item.codeVersion) office.agents[index].head.rotation.x -= pulse * .04;
        }
      }
      stage.dataset.handoffCount = String(flights.length);
      stage.dataset.traceKind = actions.map(item => item.kind).join(',');
      element('handoff-note').textContent = actions.length ? actions.map(item => `${item.from !== null ? `${humanAgents[item.from].name} → ${humanAgents[item.to].name} · ` : ''}${item.eventType} · ${item.label} · ${item.occurredAt}`).join(' / ')
        : '새 실제 Trace 이벤트를 기다립니다. 현재 Step과 과거 이벤트 연출은 구분됩니다.';
      const info = handoffs.info(), feed = runDisplayState?.trace;
      element('trace-notice').textContent = runDisplayState?.errors?.trace ? '신규 Trace 수집 실패 · 커서와 대기 큐를 보존하고 재시도합니다.'
        : `Trace ${feed?.cursor ?? '-'} / ${feed?.total ?? '-'}행 확인 · 연출 대기 ${info.pending} · 처리 ${info.completed}`
          + (!info.ready ? ' · 조회 완료 대기' : !enabled ? ' · 모션 일시정지' : '');
      const shown = [...info.recent, ...info.current].slice(-8);
      const key = shown.map(item => item.eventId).join('|');
      if (key !== traceUiKey) {
        traceUiKey = key;
        element('trace-recent').replaceChildren(...shown.map(item => {
          const row = document.createElement('li');
          row.textContent = `${item.eventType} · ${item.label} · ${item.agent !== null ? humanAgents[item.agent].name : item.actor} · ${item.occurredAt}`
            + (item.codeVersion !== null ? ` · 코드 버전 ${item.codeVersion}` : '')
            + (item.issueId ? ` · Issue ${item.issueId}` : '')
            + (item.outputArtifactIds?.length ? ` · 산출물 ${item.outputArtifactIds.length}개` : '');
          return row;
        }));
      }
    }
    runController.setSnapshot(runDisplayState);
    successCeremony.observe(runDisplayState, displayMode);
    let managementTime = 0, managementRunId = null;
    let lastManagement = null;
    function actualRows(delta = 0, enabled = false) {
      if (managementRunId !== runDisplayState?.runId) { managementTime = 0; managementRunId = runDisplayState?.runId; lastManagement = null; }
      let manager = managementDisplay(runDisplayState);
      if (manager.refreshing && lastManagement) manager = { ...lastManagement, refreshing: true };
      else if (manager.displayState !== 'unknown') lastManagement = manager;
      if (enabled && manager.displayState === 'working') managementTime += delta;
      return [...runController.advance(delta, enabled), { ...manager, motionTime: managementTime, poseTime: managementTime }];
    }
    let actualAgents = actualRows();
    let actualPlayback = null;
    let wasActualFinale = false;
    var liveRows = office.agents.map(() => {
      const row = document.createElement('li');
      element('live-agents').append(row);
      return row;
    });
    var background = new Eo().load("/agent-office-3d/office-background.png");
    background.colorSpace = ct;
    Ku(office, background);
    const agentCamera = createAgentCloseupCamera(office, engine);
    const displayRole = (index) => humanAgents[index].name;
    const stateAt = (index, atTime) => index === orchestratorIndex ? atTime >= En ? 'completed' : atTime > 0 ? 'working' : 'waiting' : ci(index, atTime);
    var bubble = element("bubble"),
      cameraButton = element("home"),
      celebration = element("celebration"),
      reducedMotion = matchMedia("(prefers-reduced-motion: reduce)"),
      timeline = element("timeline"),
      playButton = element("play"),
      stepButtons = [...root.querySelectorAll(".office3d-step")].sort((a, b) => Number(a.dataset.agentIndex) - Number(b.dataset.agentIndex)),
      agentLabels = office.agents.map((i, e) => {
        let t = document.createElement("button");
        return t.className = `office3d-agent-label${e === orchestratorIndex ? " is-lead" : ""}`, t.innerHTML = `<span class="status-dot"></span><span>${displayRole(e)}</span>${e === orchestratorIndex ? '<b class="office3d-lead-tag">리더</b>' : ''}<small></small>`, listen(t, "click", () => {
          selectedAgent = e;
          statusOpen = true;
          element("vector-panel").hidden = false;
          element("vectors").setAttribute("aria-pressed", "true");
          updateUi();
        }), stage.append(t), t;
      }),
      stateLabels = {
        waiting: "대기 중",
        working: "작업 중",
        handoff: "결과 전달",
        resting: "정리 중",
        completed: "휴식 중"
      },
      roleColors = ["#9bafc2", "#b1a0c5", "#93ae98", "#8fa7c8", "#c7aa68"],
      time = 0,
      playing = !1,
      speed = tr,
      autoCamera = !0,
      selectedAgent = -1,
      statusOpen = !1,
      lastFrame = performance.now(),
      lastUiFrame = -1,
      cameraTarget = homeTarget.clone(),
      request = requestText || "업무 흐름 시연",
      requestKind = al(request),
      errorMode = element("error-mode").value,
      finale = getNextCelebration().value,
      completionRecorded = false,
      previewingFinale = false,
      bubbleKey = "";
    function beginPresentation() {
      finale = getNextCelebration().value;
      completionRecorded = false;
      previewingFinale = false;
      time = 0;
    }
    function recordCompletion() {
      if (displayMode === 'actual') return;
      if (time < En || completionRecorded || previewingFinale) return;
      const selected = completeCelebration();
      finale = selected.value;
      completionRecorded = true;
      if (!selected.persisted) element("notice").textContent = '브라우저 저장소를 사용할 수 없어 새로고침 후에는 세리머니 순서가 초기화됩니다.';
    }
    function updatePlayButton() {
      playButton.textContent = playing ? "Ⅱ 일시정지" : time >= bn ? "↻ 다시 재생" : "▶ 재생", playButton.setAttribute("aria-pressed", String(playing));
    }
    function togglePlayback() {
      if (displayMode === 'actual') return;
      if (time >= bn) beginPresentation();
      playing = motionEnabled && !playing;
      updatePlayButton();
    }
    function setAutoCamera(i) {
      if (!i) {
        agentCamera.hide();
        camera.clearViewOffset();
        camera.position.copy(homeEye);
        cameraTarget.copy(homeTarget);
        camera.zoom = Math.min(1, camera.aspect / 1.15);
        camera.updateProjectionMatrix();
      }
      autoCamera = i, controls.enabled = !autoCamera, cameraButton.textContent = autoCamera ? "전체 보기" : "자동 추적", cameraButton.setAttribute("aria-pressed", String(autoCamera)), controls.target.copy(cameraTarget), controls.update();
    }
    setAutoCamera(!0);
    listen(cameraButton, "click", () => setAutoCamera(!autoCamera));
    listen(playButton, "click", togglePlayback);
    listen(timeline, "input", () => {
      if (displayMode === 'actual') return;
      time = Number(timeline.value), recordCompletion(), updatePlayButton(), updateUi();
    });
    listen(element("speed"), "change", i => speed = Number(i.target.value));
    // 상단 atelier 요청 폼과 별개인 시각적 시연만 다시 시작합니다.
    listen(element("restart"), "click", () => {
      if (displayMode === 'actual') return;
      errorMode = element("error-mode").value;
      beginPresentation();
      playing = motionEnabled;
      selectedAgent = -1;
      setAutoCamera(true);
      updatePlayButton();
      updateUi();
    });
    listen(element("error-mode"), "change", event => {
      if (displayMode === 'actual') return;
      errorMode = event.target.value;
      updateUi();
    });
    listen(element("preview-finale"), "click", () => {
      if (displayMode === 'actual') return;
      // 완료한 발표는 같은 포즈를 다시 보여주고, 미완료 발표는 다음 포즈만 미리 봅니다.
      if (!completionRecorded) {
        finale = getNextCelebration().value;
        previewingFinale = true;
      }
      time = En;
      playing = motionEnabled;
      setAutoCamera(true);
      updatePlayButton();
      updateUi();
    });
    listen(element("reset-finale"), "click", () => {
      if (displayMode === 'actual') return;
      const persisted = resetCelebrationOrder();
      beginPresentation();
      playing = false;
      setAutoCamera(true);
      element("notice").textContent = persisted
        ? '리허설 순서를 초기화했습니다. 다음 발표 완료 시 1번 포즈를 표시합니다.'
        : '현재 페이지의 순서를 1번으로 초기화했습니다. 브라우저 저장소는 사용할 수 없습니다.';
      updatePlayButton();
      updateUi();
    });
    listen(element('success-play'), 'click', () => {
      if (displayMode !== 'actual' || handoffs.busy() || !handoffs.info().ready) return;
      if (successCeremony.start({ ready: sceneReady(), motionEnabled: motionAllowed(), manual: true })) {
        selectedAgent = -1; setAutoCamera(true);
      }
      updateUi();
    });
    listen(element('success-reset'), 'click', () => {
      if (displayMode !== 'actual' || successCeremony.info().playback && !successCeremony.info().playback.completed) return;
      if (!window.confirm('실제 성공 세리머니 순서와 처리한 Run 기록을 초기화할까요? 목업 기록은 유지됩니다.')) return;
      if (successCeremony.reset()) { actualPlayback = null; restoreActualPose(); }
      updateUi();
    });
    stepButtons.forEach((i, e) => listen(i, "click", () => {
      if (displayMode === 'actual') {
        selectedAgent = e; setAutoCamera(true); updateUi(); return;
      }
      if (e === orchestratorIndex) { selectedAgent = e; setAutoCamera(true); updateUi(); return; }
      time = At[e].start + .4, selectedAgent = e, playing = motionEnabled, setAutoCamera(!0), updatePlayButton(), updateUi();
    }));
    listen(element("fullscreen"), "click", () => {
      const action = document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen?.();
      action?.catch(() => { element("notice").textContent = '이 브라우저에서는 전체 화면을 열 수 없습니다.'; });
    });
    listen(element("vectors"), "click", i => {
      statusOpen = !statusOpen, element("vector-panel").hidden = !statusOpen, i.currentTarget.setAttribute("aria-pressed", String(statusOpen)), updateUi();
    });
    listen(root, "keydown", i => {
      i.code === "Space" && !["INPUT", "SELECT", "BUTTON", "TEXTAREA"].includes(document.activeElement.tagName) && (i.preventDefault(), togglePlayback());
    });
    function resize() {
      renderer.setSize(Math.max(1, stage.clientWidth), Math.max(1, stage.clientHeight), !1), camera.aspect = Math.max(1, stage.clientWidth) / Math.max(1, stage.clientHeight), camera.zoom = Math.min(1, camera.aspect / 1.45), camera.updateProjectionMatrix();
    }
    var resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(stage);
    resize();
    const referenceCamera = new Dt(39, 1, .1, 110);
    const projectedPoint = new P();
    const focusPoint = new P();
    const digitalView = { zoom: 1, x: 0, y: 0 };
    // 전체 보기·세리머니는 사진의 원래 투영을 유지합니다.
    // 개별 캐릭터를 볼 때는 아래 공통 정면 카메라로 분기합니다.
    function updateCamera(bubbleInfo, delta, finaleTime = null) {
      let active = -1;
      if (!autoCamera) {
        agentCamera.hide();
        controls.update();
        cameraTarget.copy(controls.target);
        return active;
      }
      if (displayMode === 'actual' && finaleTime === null) active = selectedAgent >= 0 ? selectedAgent : bubbleInfo?.agent ?? -1;
      else if (displayMode === 'mock' && selectedAgent === orchestratorIndex && time < En) active = orchestratorIndex;
      else if (bubbleInfo && time < At[bubbleInfo.agent].end + .5 * pt) active = bubbleInfo.agent;
      if (active >= 0 && (displayMode === 'actual' || time < En)) {
        cameraTarget.copy(agentCamera.update(camera, stage.clientWidth, stage.clientHeight, active));
        return active;
      }
      agentCamera.hide();
      // 폭이 좁아져도 양쪽의 자리와 캐릭터가 함께 보이도록 여유를 둡니다.
      const baseZoom = Math.min(1, camera.aspect / 1.45);
      referenceCamera.aspect = camera.aspect;
      referenceCamera.zoom = baseZoom;
      referenceCamera.position.copy(homeEye);
      referenceCamera.lookAt(homeTarget);
      referenceCamera.updateMatrixWorld();
      referenceCamera.updateProjectionMatrix();
      const finaleCamera = finaleTime !== null || displayMode === 'mock' && time >= En;
      const agents = finaleCamera ? office.agents : active >= 0 ? [office.agents[active]] : [];
      let zoom = baseZoom, offsetX = 0, offsetY = 0;
      if (agents.length) {
        let left = Infinity, right = -Infinity, top = -Infinity, bottom = Infinity;
        for (const agent of agents) {
          for (const x of [-.85,.85]) for (const y of [finaleCamera ? 0 : 1.15,3.75]) for (const z of [-.7,.7]) {
            focusPoint.set(x,y,z);
            agent.g.localToWorld(focusPoint).project(referenceCamera);
            left = Math.min(left, focusPoint.x); right = Math.max(right, focusPoint.x);
            top = Math.max(top, focusPoint.y); bottom = Math.min(bottom, focusPoint.y);
          }
        }
        const factor = Math.max(1, Math.min(2.3, 1.7 / Math.max(.01,right-left), 1.3 / Math.max(.01,top-bottom)));
        zoom = baseZoom * factor;
        offsetX = (left+right)/2 * factor * stage.clientWidth/2;
        offsetY = (-.20 - (top+bottom)/2 * factor) * stage.clientHeight/2;
      }
      // 원본 사진의 바깥까지 화면을 밀지 않습니다. 사진보다 좁은 화면에서는
      // 배경을 가운데 둔 채 자연스러운 여백을 유지합니다.
      const corners = office.backdrop.geometry.getAttribute('position');
      let photoLeft = Infinity, photoRight = -Infinity, photoTop = -Infinity, photoBottom = Infinity;
      for (let index = 0; index < corners.count; index++) {
        focusPoint.fromBufferAttribute(corners, index);
        office.backdrop.localToWorld(focusPoint).project(referenceCamera);
        photoLeft = Math.min(photoLeft, focusPoint.x); photoRight = Math.max(photoRight, focusPoint.x);
        photoBottom = Math.min(photoBottom, focusPoint.y); photoTop = Math.max(photoTop, focusPoint.y);
      }
      const ratio = zoom / baseZoom;
      const minX = (photoLeft * ratio + 1) * stage.clientWidth / 2;
      const maxX = (photoRight * ratio - 1) * stage.clientWidth / 2;
      const minY = (1 - photoTop * ratio) * stage.clientHeight / 2;
      const maxY = (-1 - photoBottom * ratio) * stage.clientHeight / 2;
      offsetX = minX <= maxX ? Qt.clamp(offsetX, minX, maxX) : (minX + maxX) / 2;
      offsetY = minY <= maxY ? Qt.clamp(offsetY, minY, maxY) : (minY + maxY) / 2;
      const moving = displayMode === 'actual' ? motionEnabled && !reducedMotion.matches : playing && motionEnabled;
      const blend = !moving ? 1 : 1 - Math.exp(-delta * 2.8);
      digitalView.zoom = Qt.lerp(digitalView.zoom, zoom, blend);
      digitalView.x = Qt.lerp(digitalView.x, offsetX, blend);
      digitalView.y = Qt.lerp(digitalView.y, offsetY, blend);
      camera.position.copy(homeEye);
      cameraTarget.copy(homeTarget);
      camera.lookAt(homeTarget);
      camera.zoom = digitalView.zoom;
      camera.setViewOffset(stage.clientWidth,stage.clientHeight,digitalView.x,digitalView.y,stage.clientWidth,stage.clientHeight);
      camera.updateMatrixWorld();
      return active;
    }
    function projectPoint(i, e, t) {
      return projectedPoint.set(i, e, t).project(camera), {
        x: (projectedPoint.x + 1) * stage.clientWidth / 2,
        y: (1 - projectedPoint.y) * stage.clientHeight / 2,
        z: projectedPoint.z
      };
    }
    var cornerPoint = new P();
    function faceBounds({
      head: i,
      g: e,
      i: t
    }, n = !1) {
      let s = {
          left: 1 / 0,
          right: -1 / 0,
          top: 1 / 0,
          bottom: -1 / 0
        },
        r = t === 0 ? 1.13 : .88,
        o = n ? .2 : 0;
      for (let a of [-.78, .78]) for (let c of [-.68, r + o]) for (let l of [-.64, .66]) {
        n ? cornerPoint.set(a, c + 2.5, l - .51).applyMatrix4(e.matrixWorld) : cornerPoint.set(a, c, l).applyMatrix4(i.matrixWorld);
        let h = projectPoint(cornerPoint.x, cornerPoint.y, cornerPoint.z);
        h.z < -1 || h.z > 1 || (s.left = Math.min(s.left, h.x), s.right = Math.max(s.right, h.x), s.top = Math.min(s.top, h.y), s.bottom = Math.max(s.bottom, h.y));
      }
      return s;
    }
    function positionBubble(i) {
      let e = faceBounds(office.agents[i], !0),
        t = stage.clientWidth,
        n = stage.clientHeight,
        s = (e.left + e.right) / 2;
      if (!Number.isFinite(s) || e.right < 0 || e.left > t || e.bottom < 0 || e.top > n) {
        bubble.hidden = !0;
        return;
      }
      bubble.hidden = !1;
      let r = 12,
        o = bubble.offsetWidth,
        c = o / 2,
        l = Qt.clamp(s, c + 14, t - c - 14);
      bubble.style.left = l + "px"; bubble.style.top = r + "px";
      let d = Qt.clamp(50 + 100 * (s - l) / o, 8, 92);
      bubble.style.setProperty("--popup-tail", d + "%");
    }
    function positionOverlays(i, e, finaleTime = null) {
      bubble.hidden = displayMode === 'actual' || !i;
      if (displayMode === 'mock' && i) positionBubble(i.agent);
      for (let t = 0; t < office.agents.length; t++) {
        let n = office.agents[t],
          s = finaleTime !== null || displayMode === 'mock' && time >= En || e >= 0 && e !== t || i?.agent === t;
        if (agentLabels[t].hidden = s, agentLabels[t].style.display = s ? "none" : "flex", s) continue;
        let r = n.g.localToWorld(new P(0, 3.65, -.4)),
          o = projectPoint(...r.toArray());
        const halfLabel = agentLabels[t].offsetWidth / 2 + 6;
        const labelX = Qt.clamp(o.x, halfLabel, stage.clientWidth - halfLabel);
        agentLabels[t].style.transform = `translate(${labelX}px,${o.y}px) translate(-50%,-100%)`;
      }
      const pose = actualPlayback?.pose ?? finale;
      const shownTime = finaleTime ?? time;
      celebration.hidden = displayMode === 'actual' && finaleTime === null || shownTime < (pose === 'team' ? vs + .5 : En + 1.4);
      if (!celebration.hidden) {
        celebration.style.top = '12px';
        celebration.style.setProperty('--celebration-scale', '1');
      }
      if (i && displayMode === 'mock') element("bubble-progress").style.transform = `scaleX(${i.progress})`;
    }
    function actualBubble() {
      const running = actualAgents.map((agent, index) => index < orchestratorIndex && agent.displayState === 'working' ? index : -1).filter(index => index >= 0);
      const index = selectedAgent >= 0 ? selectedAgent : running.length === 1 ? running[0] : -1;
      if (index < 0) return null;
      const agent = actualAgents[index];
      return { agent: index, tone: agent.displayState === 'failed' ? 'error' : 'normal',
        title: agent.label, detail: agent.refreshing ? '갱신 중 · 마지막으로 확인된 상태입니다.'
          : agent.displayState === 'unknown' ? '조회 결과로 현재 상태를 확인할 수 없습니다. Agent 실패를 의미하지 않습니다.'
            : agent.step ? `Step 상태: ${agent.step.status} · 시도 ${agent.step.attempt} · 코드 버전 ${agent.step.codeVersion ?? '-'}` : agent.managementServer ? '리더 · Run 상태에 따른 실행 관리 표시입니다. 별도 작업 Step이 아닙니다.' : '아직 생성된 Step이 없습니다.' };
    }
    function updateActualUi() {
      bubble.hidden = true;
      const popups = buildOfficeRunPopups(runDisplayState, actualAgents, handoffs.info(), { mode: displayMode, ceremony: Boolean(actualPlayback), previous: previousPopups });
      previousPopups = { runId: runDisplayState?.runId, popups };
      const notice = popupResultNotice(runDisplayState);
      const popupKey = JSON.stringify([popups, notice]);
      if (popupKey !== actualPopupKey) {
        actualPopupKey = popupKey;
        renderOfficeRunPopups(root, popups, notice);
      }
      const refreshing = actualAgents.some(agent => agent.refreshing);
      const unknown = actualAgents.some(agent => agent.displayState === 'unknown');
      const queryFailed = runDisplayState?.errors?.run || runDisplayState?.errors?.steps;
      element('live-notice').textContent = `Run ${runDisplayState?.runId ?? '-'} · ` + (refreshing
        ? '갱신 중 · 확인된 상태가 있으면 그대로 표시합니다.' : queryFailed
          ? '조회 오류 · Agent 작업 실패가 아닙니다. 수동 새로고침으로 다시 확인해주세요.'
          : unknown ? '상태 확인 불가 · 불명확한 Step을 임의로 실행하거나 실패 처리하지 않습니다.'
            : `Run 상태 ${runDisplayState?.run.status ?? '-'} · 최종 판정 ${runDisplayState?.run.verdict ?? '아직 최종 판정 없음'}`);
      if (runDisplayState?.errors?.events) element('live-notice').textContent += ' · Event 조회 실패(Step 표시는 유지).';
      const success = successCeremony.info();
      const playable = sceneReady() && motionAllowed();
      element('success-play').disabled = !success.eligible || Boolean(success.processed) || !playable || handoffs.busy() || !handoffs.info().ready;
      element('success-reset').disabled = Boolean(success.playback && !success.playback.completed);
      element('success-notice').textContent = success.storageError
        ? '실제 성공 기록을 읽거나 저장할 수 없어 재생하지 않습니다. 저장소 설정을 확인한 뒤 버튼으로 다시 시도해주세요.'
        : success.playback ? `${success.playback.pose} · ${success.playback.completed ? '세리머니 재생 완료' : '성공 세리머니 재생 중'}`
          : success.processed ? `${success.processed.pose} · 이미 재생을 시작한 Run입니다. 중복 재생하지 않습니다.${success.interrupted ? ' 조회 오류 등으로 재생이 중단되었습니다.' : ''}`
            : success.eligible ? `${success.nextPose} · ${success.autoPending ? '3D·모션 준비 후 자동 재생 대기' : '버튼을 눌러 성공 세리머니를 재생해주세요.'}${!playable ? ' 3D 준비 및 모션 켜기가 필요합니다.' : ''}`
              : runDisplayState?.errors?.architecture ? '실행 방식 조회 실패 · 성공 세리머니를 실행하지 않습니다.'
                : runDisplayState?.architecture && runDisplayState.architecture !== 'MULTI_AGENT' ? 'MULTI_AGENT Run이 아니므로 성공 세리머니를 실행하지 않습니다.'
                  : '모든 조회와 MULTI_AGENT·FINISHED·SUCCESS 확인 후 성공 세리머니를 사용할 수 있습니다.';
      actualAgents.forEach((agent, index) => {
        const suffix = agent.refreshing ? agent.hasConfirmedState ? ' · 갱신 중(마지막 확인 상태)' : ' · 갱신 중' : '';
        const label = agentLabels[index];
        for (const target of [label, liveRows[index]]) {
          target.dataset.state = agent.displayState;
          target.dataset.officeAgentRole = agent.agentRole;
          target.dataset.motionTime = agent.motionTime.toFixed(3);
          target.dataset.expression = agent.expression;
        }
        liveRows[index].textContent = `${displayRole(index)} · ${agent.label}${suffix}`;
        label.querySelector('small').textContent = agent.label + suffix;
        label.classList.toggle('selected', selectedAgent === index);
        stepButtons[index].classList.toggle('current', agent.displayState === 'working');
        stepButtons[index].classList.toggle('done', agent.displayState === 'succeeded');
      });
      const info = actualPlayback ? null : actualBubble();
      if (actualPlayback) {
        element('finale-source').textContent = '실제 멀티 에이전트 · SUCCESS';
        element('finale-title').textContent = '협업 최종 성공';
        element('finale-caption').textContent = finaleOptions.find(item => item.value === actualPlayback.pose).label;
        element('finale-note').textContent = `Run ${actualPlayback.runId}`;
      }
      if (statusOpen) {
        const index = selectedAgent >= 0 ? selectedAgent : info?.agent ?? 0;
        const agent = actualAgents[index], model = office.agents[index];
        element('selected').textContent = displayRole(index) + ' · 실제 Run';
        element('vector-data').textContent = `상태 ${agent.label}\nStep ${agent.step?.workflowStepId ?? '-'}\n작업 모션 시간 ${agent.motionTime.toFixed(2)}초 (서버 진행률 아님)\n오른팔 회전 [${model.arms[1].shoulder.rotation.toArray().slice(0, 3).map(value => value.toFixed(3)).join(', ')}]\n조회 중 ${agent.refreshing ? '예' : '아니오'}`;
      }
    }
    function updateUi() {
      if (displayMode === 'actual') { updateActualUi(); return; }
      element('actual-popups').hidden = true;
      actualPopupKey = '';
      previousPopups = null;
      bubble.dataset.mode = 'mock';
      element('finale-source').textContent = '목업 시연 · 리더 + 4개 역할';
      element('finale-title').textContent = '협업 완료 시연';
      element('finale-note').textContent = '실제 코드 생성·검증 결과는 아닙니다.';
      let i = Zo(time, requestKind, errorMode),
        e = office.agents.slice(0, orchestratorIndex).filter(n => ci(n.i, time) === "completed").length,
        t = e > 0 ? "다음 에이전트가 작업을 준비하고 있어요." : "요청을 받아 업무를 준비하고 있어요.";
      element("finale-caption").textContent = finaleOptions.find((item) => item.value === finale).label;
      const next = getNextCelebration();
      element("finale-order").textContent = completionRecorded
        ? `이번 발표 · ${finaleOptions.find((item) => item.value === finale).label} / 다음 ${next.number}번`
        : `${previewingFinale ? '미리보기' : '다음 세리머니'} · ${finaleOptions.find((item) => item.value === finale).label}`;
      element("caption").textContent = e === 4 ? "업무 완료 시연 · " + finaleOptions.find((item) => item.value === finale).label : i ? `${request} \xB7 ${i.title}` : t, element("stepcount").textContent = `작업 완료 ${e} / 4 · 리더 1`;
      for (let n = 0; n < office.agents.length; n++) {
        let s = stateAt(n, time);
        agentLabels[n].dataset.state = s, agentLabels[n].querySelector("small").textContent = stateLabels[s], agentLabels[n].style.setProperty("--role-color", roleColors[n]), agentLabels[n].classList.toggle("selected", selectedAgent === n), stepButtons[n].classList.toggle("current", i?.agent === n), stepButtons[n].classList.toggle("done", s === "completed");
      }
      if (i) {
        let n = `${requestKind}|${i.agent}|${i.tone}|${i.title}|${i.detail}`;
        if (n !== bubbleKey) {
          bubbleKey = n;
          let s = i.tone === "error" && (bubble.dataset.tone !== "error" || bubble.dataset.agent !== String(i.agent));
          bubble.dataset.tone = i.tone, bubble.dataset.agent = String(i.agent), bubble.classList.remove("fresh", "warning-enter"), element("bubble-role").textContent = displayRole(i.agent) + (i.tone === "error" ? " \xB7 오류 경고" : " \xB7 " + stateLabels[i.state]), element("bubble-title").textContent = i.title, element("bubble-detail").textContent = i.detail, bubble.offsetWidth, bubble.classList.add("fresh"), s && bubble.classList.add("warning-enter");
        }
      } else bubbleKey = "", bubble.dataset.tone = "normal", bubble.classList.remove("fresh", "warning-enter");
      if (timeline.value = String(time), timeline.style.setProperty("--played", time / bn * 100 + "%"), element("clock").textContent = `${String(Math.floor(time)).padStart(2, "0")} / ${bn}초`, statusOpen) {
        let n = selectedAgent < 0 ? i?.agent ?? 0 : selectedAgent,
          s = office.agents[n];
        element("selected").textContent = displayRole(n), element("vector-data").textContent = `position  [${s.g.position.toArray().map(r => r.toFixed(2)).join(", ")}]
rotation  [${s.upper.rotation.toArray().slice(0, 3).map(r => r.toFixed(2)).join(", ")}]
state     ${stateAt(n, time)}
좌표 단위 m \xB7 회전 단위 rad`;
      }
    }
    function motionAllowed() { return motionEnabled && !reducedMotion.matches; }
    function sceneReady() {
      return renderedOnce && !disposed && root.isConnected && stage.clientWidth > 0 && stage.clientHeight > 0
        && document.visibilityState === 'visible' && element('loading').hidden
        && office.agents.length === humanAgents.length && !renderer.getContext().isContextLost();
    }
    function restoreActualPose() { updateOfficePose(0); acting.update(0, finale, null, false); wasActualFinale = false; }
    function renderFrame(i) {
      let e = Math.min(.1, Math.max(0, (i - lastFrame) / 1e3));
      lastFrame = i;
      if (displayMode === 'actual') {
        const ready = sceneReady(), enabled = motionAllowed();
        if (!handoffs.busy() && handoffs.info().ready && successCeremony.start({ ready, motionEnabled: enabled })) { selectedAgent = -1; setAutoCamera(true); }
        actualPlayback = successCeremony.advance(e * tr, { ready, motionEnabled: enabled, duration: bn - En });
        actualAgents = actualRows(e, enabled);
        if (actualPlayback) {
          actualPapers.forEach(paper => { paper.visible = false; });
          const performanceTime = En + actualPlayback.elapsed;
          updateOfficePose(performanceTime);
          acting.update(performanceTime, actualPlayback.pose, null, enabled);
          wasActualFinale = true;
        } else {
          if (wasActualFinale) restoreActualPose();
          actualAgents.forEach((agent, index) => {
            const model = office.agents[index];
            const pose = getOfficeAgentPose(index === orchestratorIndex ? 0 : index, agent.poseTime, agent.poseState);
            pose.arm[0][2] -= .08; pose.arm[1][2] += .08;
            applyOfficeAgentPose(model, pose);
            model.eyes.scale.y = 1;
          });
          acting.updateActual(actualAgents);
          updateActualHandoffs(e, enabled);
        }
        const info = actualPlayback ? null : actualBubble();
        stage.dataset.finale = actualPlayback?.pose ?? 'none';
        stage.dataset.actualCeremony = actualPlayback ? actualPlayback.completed ? 'completed' : 'playing' : 'none';
        stage.dataset.actualCeremonyRun = actualPlayback?.runId ?? '';
        stage.dataset.actualCeremonyTime = String(actualPlayback?.elapsed ?? 0);
        stage.dataset.expression = actualAgents.some(agent => agent.expression === 'worried') ? 'worried' : 'normal';
        const performanceTime = actualPlayback ? En + actualPlayback.elapsed : null;
        const active = updateCamera(info, e, performanceTime);
        camera.updateProjectionMatrix();
        if (i - lastUiFrame > 75) { updateUi(); lastUiFrame = i; }
        positionOverlays(info, active, performanceTime);
        renderer.render(scene, camera);
        renderedOnce = true;
        frame = requestAnimationFrame(renderFrame);
        return;
      }
      playing && (time = Math.min(bn, time + e * speed), time === bn && (playing = !1, updatePlayButton()));
      stage.dataset.actualCeremony = 'none';
      recordCompletion();
      updateOfficePose(time);
      let t = Zo(time, requestKind, errorMode);
      const expression = acting.update(time, finale, t, motionEnabled && !reducedMotion.matches);
      bubble.dataset.expression = expression;
      stage.dataset.expression = expression;
      element("bubble-emotion").textContent = ({worried:'난감',angry:'화남',sweat:'진땀'})[expression] || '';
      stage.dataset.finale = finale;
      const n = updateCamera(t, e * speed);
      camera.updateProjectionMatrix(), i - lastUiFrame > 75 && (updateUi(), lastUiFrame = i), positionOverlays(t, n), renderer.render(scene, camera), renderedOnce = true, frame = requestAnimationFrame(renderFrame);
    }
    function setRunDisplay(mode, snapshot) {
      const changed = displayMode !== mode || runDisplayState?.runId !== snapshot?.runId;
      displayMode = mode;
      runDisplayState = snapshot;
      successCeremony.observe(snapshot, mode);
      actualPlayback = successCeremony.info().playback;
      runController.setSnapshot(snapshot);
      handoffs.observe(snapshot, mode);
      actualAgents = actualRows();
      if (changed) {
        actualPapers.forEach(paper => office.workspace.remove(paper));
        actualPapers.clear();
        traceUiKey = '';
        actualPopupKey = '';
        previousPopups = null;
        element('trace-recent').replaceChildren();
        element('trace-notice').textContent = '새 Run의 Trace 기준점을 확인하고 있습니다.';
        playing = false;
        selectedAgent = -1;
        bubbleKey = '';
        if (mode === 'actual') restoreActualPose();
      }
      updatePlayButton();
      updateUi();
    }
    if (displayMode === 'actual') { updateOfficePose(0); acting.update(0, finale, null, false); }
    updatePlayButton();
    updateUi();
    frame = requestAnimationFrame(renderFrame);
    element("loading").hidden = true;
    return {
      setRequest(text) {
        request = text || '업무 흐름 시연';
        requestKind = al(request);
        updateUi();
      },
      setMotion(enabled) {
        motionEnabled = enabled;
        if (!enabled) {
          playing = false;
          updatePlayButton();
        }
      },
      setRunDisplay,
      dispose
    };
  } catch (error) {
    dispose();
    throw error;
  }
}
