#!/usr/bin/env node
'use strict';

/**
 * MINICRAFT 客户端回归测试。
 *
 * 不启动浏览器；从 assets/minicraft.js 提取纯函数，在隔离 VM 中验证轨道、
 * 模板、信号、站立物理、快捷键、图标、备份校验和接触面规则。
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const SOURCE = fs.readFileSync(path.join(ROOT, 'assets', 'minicraft.js'), 'utf8');
let passed = 0;

function assert(condition, message = 'assertion failed') {
  if (!condition) throw new Error(message);
}

function near(actual, expected, epsilon = 1e-6, message = '') {
  assert(Math.abs(actual - expected) <= epsilon, message || `${actual} != ${expected} ± ${epsilon}`);
}

function extractFunction(name) {
  const start = SOURCE.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function not found: ${name}`);
  const bodyStart = SOURCE.indexOf('{', start);
  let depth = 0;
  let i = bodyStart;
  for (; i < SOURCE.length; i++) {
    if (SOURCE[i] === '{') depth++;
    else if (SOURCE[i] === '}' && --depth === 0) {
      i++;
      break;
    }
  }
  return SOURCE.slice(start, i);
}

function context(extra = {}) {
  return vm.createContext({console, ...extra});
}

function runFunctions(ctx, names) {
  for (const name of names) vm.runInContext(extractFunction(name), ctx, {filename: `function:${name}`});
}

function loadTrackCatalog(ctx) {
  const start = SOURCE.indexOf('const TRACK_SHAPES=');
  const end = SOURCE.indexOf('  function trackDirection', start);
  assert(start >= 0 && end > start, 'track catalog not found');
  const code = SOURCE.slice(start, end).replace(/^  const /gm, 'var ').replace(/^const /gm, 'var ');
  vm.runInContext(code, ctx, {filename: 'track-catalog'});
}

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`✓ ${name}`);
  } catch (error) {
    console.error(`✗ ${name}`);
    throw error;
  }
}

const GEOMETRY = [
  'trackDirection', 'turntableSelectedExit', 'turntableDirectionLabel', 'rotateTrackOffset', 'trackCells',
  'trackCenter', 'trackLocalToWorld', 'trackPorts', 'trackPortKey',
  'trackRouteExit', 'trackAllowsEntry', 'trackPathRaw', 'trackRoutePairs',
  'trackPlanningExits', 'templatePieceDefaults', 'transformTemplatePieces'
];

const EXPECTED_SHAPES = [
  'straight', 'curve', 'cross', 'switch', 'stop', 'station', 'signal', 'ramp',
  'bridge', 'power', 'lantern', 'crossing', 'oneway', 'drawbridge', 'whistle',
  'rampcurve', 'tunnel', 'turntable', 'detector', 'depot'
];

test('轨道目录顺序、尺寸与存档索引保持兼容', () => {
  const ctx = context({drawbridgeAngles: new Map()});
  loadTrackCatalog(ctx);
  runFunctions(ctx, GEOMETRY);
  assert(JSON.stringify(Array.from(ctx.TRACK_SHAPES)) === JSON.stringify(EXPECTED_SHAPES), 'track shape order changed');
  const size = shape => ctx.trackCells({id: 1, x: 10, y: 2, z: 10, rot: 0, shape, rampRise: true}).length;
  assert(size('straight') === 2, 'straight must be 1x2');
  assert(size('cross') === 1, 'cross must be 1x1');
  assert(size('ramp') === 2, 'ramp must occupy 2 cells');
  assert(size('rampcurve') === 4, 'curved ramp must occupy 2x2');
  assert(size('tunnel') === 2, 'tunnel must be 1x2');
  assert(size('turntable') === 1, 'turntable must be 1x1');
  assert(size('detector') === 1, 'detector must be 1x1');
  assert(size('depot') === 2, 'depot must be 1x2');
});

test('全部预定义模板在四个方向均无重叠、无断口', () => {
  const ctx = context({drawbridgeAngles: new Map()});
  loadTrackCatalog(ctx);
  runFunctions(ctx, GEOMETRY);
  assert(ctx.TRACK_TEMPLATES.length === 10, 'unexpected template count');
  for (const template of ctx.TRACK_TEMPLATES) {
    for (let rotation = 0; rotation < 4; rotation++) {
      const pieces = Array.from(ctx.transformTemplatePieces(template, rotation), (piece, i) => ({...piece, id: i + 1}));
      const cells = new Set();
      const ports = [];
      for (const piece of pieces) {
        for (const cell of ctx.trackCells(piece)) {
          const key = `${cell.x},${cell.y},${cell.z}`;
          assert(!cells.has(key), `${template.id} overlap ${key} rotation ${rotation}`);
          cells.add(key);
        }
        for (const port of ctx.trackPorts(piece)) ports.push({piece, port, key: ctx.trackPortKey(port)});
      }
      for (const current of ports) {
        const linked = ports.some(other => other.piece.id !== current.piece.id && other.key === current.key && other.port.dir === (current.port.dir + 2) % 4);
        assert(linked, `${template.id} loose port rotation ${rotation}`);
      }
    }
  }
  const hub = Array.from(ctx.TRACK_TEMPLATES).find(template => template.id === 'turntable_hub');
  assert(hub && hub.pieces().length === 9, 'turntable hub template missing');
  const sorter = Array.from(ctx.TRACK_TEMPLATES).find(template => template.id === 'detector_sorter');
  assert(sorter && sorter.pieces().length === 8 && sorter.pieces().some(piece => piece.shape === 'detector' && piece.detectorMode === 2), 'detector sorter template missing');
  const depot = Array.from(ctx.TRACK_TEMPLATES).find(template => template.id === 'double_depot');
  assert(depot && depot.pieces().length === 7 && depot.pieces().filter(piece => piece.shape === 'depot').length === 2, 'double depot template missing');
});

test('旋转轨四向出口、中心转向路径、方向标签、规划与图标', () => {
  const ctx = context({drawbridgeAngles: new Map(), trackPieceBusy: () => false});
  loadTrackCatalog(ctx);
  runFunctions(ctx, [...GEOMETRY, 'trackShortcutLabel', 'trackIconSvg']);
  const piece = {id: 1, x: 0, y: 1, z: 0, rot: 0, shape: 'turntable', turntableAxis: 0, turntableExit: 2};
  assert(ctx.trackPorts(piece).length === 4, 'turntable needs four ports');
  for (let entry = 0; entry < 4; entry++) assert(ctx.trackAllowsEntry(piece, entry), `entry ${entry} should be accepted`);
  assert(ctx.trackRouteExit(piece, 0) === 2, 'north entry should use selected south exit');
  assert(ctx.trackRouteExit(piece, 1) === 2, 'east entry should curve to selected south exit');
  assert(ctx.trackRouteExit(piece, 2) === 0, 'selected-side entry should continue opposite');
  assert(ctx.trackRoutePairs(piece).length === 3, 'turntable route display should expose three unique paths');
  assert(ctx.turntableDirectionLabel(piece) === '南', 'selected exit label invalid');
  const curvedMid = ctx.trackPathRaw(piece, 1, 2, 0.5);
  const center = ctx.trackCenter(piece);
  near(curvedMid.x, center.x, 0.001, 'turntable route misses center');
  near(curvedMid.z, center.z, 0.001, 'turntable route misses center');
  assert(ctx.trackPlanningExits(piece, 0).length === 3, 'free turntable should expose three planned exits');
  piece.turntableExit = 1;
  assert(ctx.turntableDirectionLabel(piece) === '东', 'east exit label invalid');
  piece.rot = 1;
  assert(ctx.turntableDirectionLabel(piece) === '北', 'rotated exit label invalid');
  assert(ctx.trackShortcutLabel(17) === '⇧8', 'turntable shortcut changed');
  assert(ctx.trackIconSvg('turntable').includes('<circle'), 'turntable icon missing');
  assert(new Set(Array.from(ctx.TRACK_SHAPES, ctx.trackIconSvg)).size === 20, 'track icons must be unique');
});

test('旋转轨交互与列车占用锁定', () => {
  let busy = false;
  let rebuilds = 0;
  let saves = 0;
  let message = '';
  const ctx = context({
    trackNetworkCache: {cached: true},
    canNetworkEdit: () => true,
    trackPieceBusy: () => busy,
    rebuildMesh: () => rebuilds++,
    toast: text => { message = text; },
    tone: () => {},
    saveGame: () => saves++,
    turntableSelectedExit: piece => Number.isInteger(piece.turntableExit) ? piece.turntableExit : (piece.turntableAxis === 1 ? 1 : 2),
    turntableDirectionLabel: piece => ['北', '东', '南', '西'][piece.turntableExit]
  });
  runFunctions(ctx, ['interactTrack']);
  const piece = {id: 9, shape: 'turntable', turntableAxis: 0, turntableExit: 2};
  ctx.interactTrack(piece);
  assert(piece.turntableExit === 3 && piece.turntableAxis === 1 && rebuilds === 1 && saves === 1, 'turntable did not rotate');
  assert(ctx.trackNetworkCache === null && message.includes('西'), 'turntable cache/message invalid');
  busy = true;
  ctx.interactTrack(piece);
  assert(piece.turntableExit === 3 && rebuilds === 1 && saves === 1, 'busy turntable rotated');
  assert(message.includes('不能转向'), 'busy warning missing');
});

test('车站自动寻路会预先调整空闲旋转轨，且不会转动占用中的转盘', () => {
  let message = '';
  const ctx = context({
    trackPieces: [], trackPortIndex: new Map(), trackCellIndex: new Map(), trackColumnIndex: new Map(),
    trackNetworkCache: null, drawbridgeAngles: new Map(), busy: false,
    trackTrain: {placed: true, running: false, pieceId: 1, entry: 0, exit: 1, t: 0.18, speed: 0, cruiseSpeed: 1.35, yaw: 0, pitch: 0, distance: 0, routePlan: [], autoService: false},
    ridingTrain: false,
    key: (x, y, z) => `${x},${y},${z}`,
    trackPieceBusy: () => ctx.busy,
    canNetworkEdit: () => true,
    trackLineForPiece: () => ({key: 1, pieces: ctx.trackPieces, stationPieces: [ctx.trackPieces[0], ctx.trackPieces[2]]}),
    trackStationDisplayName: piece => piece.stationName || '车站',
    placeTrackTrain: () => {}, syncTrackRider: () => {}, resetTrainTrail: () => {},
    rebuildMesh: () => {}, saveGame: () => {}, closeTrackPanel: () => {},
    toast: text => { message = text; }, tone: () => {}, setTimeout: () => {}
  });
  runFunctions(ctx, [
    'trackDirection', 'turntableSelectedExit', 'turntableDirectionLabel', 'rotateTrackOffset',
    'trackCells', 'trackCenter', 'trackLocalToWorld', 'trackPorts', 'trackPortKey',
    'rebuildTrackGraph', 'connectedTrackPort', 'trackRouteExit', 'trackAllowsEntry',
    'trackPathRaw', 'trackPathLength', 'setTrackTrainPose', 'trackPlanningExits',
    'planTrackRoute', 'setTrainDestination'
  ]);
  const north = {id: 1, x: 0, y: 1, z: -2, rot: 0, shape: 'station', stationName: '北站'};
  const turntable = {id: 2, x: 0, y: 1, z: 0, rot: 0, shape: 'turntable', turntableExit: 2, turntableAxis: 0};
  const east = {id: 3, x: 1, y: 1, z: 0, rot: 1, shape: 'station', stationName: '东站'};
  ctx.trackPieces.push(north, turntable, east);
  ctx.rebuildTrackGraph();
  const plan = ctx.planTrackRoute(north, east);
  const tableStep = plan && Array.from(plan.path).find(step => step.piece.id === turntable.id);
  assert(tableStep && tableStep.entry === 0 && tableStep.exit === 1, 'planner did not choose east turntable exit');
  assert(ctx.setTrainDestination(east.id) === true, 'destination dispatch failed');
  assert(turntable.turntableExit === 1 && turntable.turntableAxis === 1, 'turntable was not pre-aligned');
  assert(message.includes('自动转向1个旋转轨'), 'automatic turntable adjustment was not reported');

  turntable.turntableExit = 2;
  turntable.turntableAxis = 0;
  ctx.busy = true;
  assert(ctx.planTrackRoute(north, east) === null, 'planner rotated an occupied turntable');
});

test('感应轨计数、报站、目标绑定、岔道/转盘触发、脉冲与联机权限', () => {
  let now = 1000;
  let rebuilds = 0;
  let saves = 0;
  let message = '';
  const ctx = context({
    trackPieces: [], detectorPulseUntil: new Map(), player: {x: 1.5, z: 3.5}, netConnected: false, netRole: '', busy: false,
    performance: {now: () => now},
    trackLineForPiece: () => ({pieces: ctx.trackPieces}),
    trackPieceBusy: () => ctx.busy,
    rebuildMesh: () => rebuilds++, saveGame: () => saves++,
    toast: text => { message = text; }, tone: () => {}, setTimeout: () => {}
  });
  runFunctions(ctx, [
    'trackDirection', 'rotateTrackOffset', 'trackCells', 'trackCenter', 'trackLocalToWorld', 'trackPorts',
    'turntableSelectedExit', 'turntableDirectionLabel', 'detectorModeLabel', 'detectorTargetCandidates',
    'detectorTarget', 'detectorTargetText', 'cycleDetectorTarget', 'triggerDetector'
  ]);
  const detector = {id: 1, x: 1, y: 1, z: 3, rot: 0, shape: 'detector', detectorMode: 2, detectorCount: 0, detectorTargetId: null};
  const branch = {id: 2, x: 0, y: 1, z: 4, rot: 0, shape: 'switch', branch: false};
  const fartherBranch = {id: 3, x: 4, y: 1, z: 7, rot: 0, shape: 'switch', branch: false};
  const turntable = {id: 4, x: 2, y: 1, z: 4, rot: 0, shape: 'turntable', turntableExit: 2, turntableAxis: 0};
  ctx.trackPieces.push(detector, branch, fartherBranch, turntable);
  assert(ctx.detectorModeLabel(detector) === '岔道触发', 'detector mode label invalid');
  assert(ctx.detectorTarget(detector) === branch, 'nearest switch target not found');
  ctx.cycleDetectorTarget(detector);
  assert(detector.detectorTargetId === branch.id && ctx.detectorTargetText(detector).includes(`#${branch.id}`), 'explicit detector binding failed');
  ctx.cycleDetectorTarget(detector);
  assert(detector.detectorTargetId === fartherBranch.id, 'detector target cycling failed');
  detector.detectorTargetId = branch.id;
  ctx.triggerDetector(detector);
  assert(detector.detectorCount === 1 && branch.branch === true, 'detector did not toggle bound switch');
  assert(ctx.detectorPulseUntil.get(detector.id) === 1720, 'detector pulse duration invalid');
  assert(rebuilds >= 3 && saves >= 3 && message.includes('弯道'), 'detector feedback invalid');

  detector.detectorMode = 3;
  detector.detectorTargetId = turntable.id;
  now = 1800;
  assert(ctx.detectorModeLabel(detector) === '转盘触发' && ctx.detectorTarget(detector) === turntable, 'turntable detector target invalid');
  ctx.triggerDetector(detector);
  assert(turntable.turntableExit === 3 && turntable.turntableAxis === 1, 'detector did not rotate turntable');
  assert(message.includes('出口已转向西'), 'turntable detector feedback invalid');
  ctx.busy = true;
  ctx.triggerDetector(detector);
  assert(turntable.turntableExit === 3 && message.includes('正在被占用'), 'busy turntable was rotated');
  ctx.busy = false;

  detector.detectorMode = 1;
  now = 2000;
  ctx.triggerDetector(detector);
  assert(detector.detectorCount === 4 && branch.branch === true, 'report mode changed switch');
  assert(ctx.detectorModeLabel(detector) === '报站提示' && message.includes('第 4 次'), 'report mode feedback invalid');

  detector.detectorMode = 0;
  ctx.triggerDetector(detector);
  assert(detector.detectorCount === 5 && ctx.detectorModeLabel(detector) === '仅计数', 'count mode invalid');
  ctx.netConnected = true;
  ctx.netRole = 'guest';
  now = 3000;
  const guestCount = detector.detectorCount;
  ctx.triggerDetector(detector);
  assert(detector.detectorCount === guestCount && branch.branch === true, 'guest duplicated authoritative detector action');
  assert(ctx.detectorPulseUntil.get(detector.id) === 3420, 'guest detector pulse missing');
  ctx.netConnected = false;
  ctx.netRole = '';

  branch.x = 40;
  branch.z = 40;
  fartherBranch.x = 45;
  fartherBranch.z = 45;
  detector.detectorTargetId = null;
  detector.detectorMode = 2;
  assert(ctx.detectorTarget(detector) === null, 'out-of-range switch was selected');
  assert(SOURCE.includes("piece.shape==='detector'&&!trackTrain.whistleServed"), 'primary train detector hook missing');
  assert(SOURCE.includes("piece.shape==='detector'&&!train.whistleServed"), 'extra train detector hook missing');
  assert(SOURCE.includes("if(lookedTrack?.shape==='detector'&&shifted)"), 'Shift+E detector binding hook missing');
});

test('木质车库自动停车、直接通过开关与主/附加列车挂钩', () => {
  const depot = {id: 1, x: 0, y: 1, z: 0, rot: 0, shape: 'depot', depotStop: true};
  const train = {placed: true, pieceId: 1, entry: 0, exit: 1, t: 0.4, speed: 1, cruiseSpeed: 1, running: true, wait: 0, distance: 0, stationServed: false, powerServed: false, whistleServed: false};
  const ctx = context({
    trackPieces: [depot],
    trainObstacleDistance: () => null, trainPedestrianDistance: () => null,
    extraTrainSpeedLimit: () => 1,
    trackPathLength: () => 1,
    setExtraTrainPose: () => {},
    signalIsGreen: () => true,
    triggerDetector: () => {},
    connectedTrackPort: () => null,
    trackAllowsEntry: () => true,
    trackClearanceBlocked: () => false,
    player: {x: 100, z: 100}, tone: () => {}, setTimeout: () => {}
  });
  runFunctions(ctx, ['updateExtraTrain']);
  ctx.updateExtraTrain(train, 0.2);
  assert(train.t === 0.5 && train.running === false && train.speed === 0 && train.stationServed === true, 'extra train did not park in depot');

  let rebuilds = 0;
  let saves = 0;
  let message = '';
  const controls = context({
    canNetworkEdit: () => true,
    rebuildMesh: () => rebuilds++, saveGame: () => saves++,
    toast: text => { message = text; }, tone: () => {}
  });
  runFunctions(controls, ['interactTrack']);
  controls.interactTrack(depot);
  assert(depot.depotStop === false && rebuilds === 1 && saves === 1 && message.includes('直接通过'), 'depot pass-through toggle failed');
  assert(SOURCE.includes("piece.shape==='depot'&&piece.depotStop!==false&&!trackTrain.stationServed"), 'primary train depot hook missing');
  assert(SOURCE.includes("piece.shape==='depot'&&piece.depotStop!==false&&!train.stationServed"), 'extra train depot hook missing');
  assert(SOURCE.includes("piece.shape==='depot'?'车库保护'"), 'depot signal protection missing');
});

test('车站支持1/2/4/8秒停靠时刻与主/附加列车调度', () => {
  let updates = 0;
  let saves = 0;
  let message = '';
  const controls = context({
    updateTrackPanel: () => updates++, saveGame: () => saves++,
    toast: text => { message = text; }, tone: () => {},
    trackStationDisplayName: piece => piece.stationName || '车站'
  });
  runFunctions(controls, ['stationDwellSeconds', 'cycleStationDwell']);
  const station = {id: 1, shape: 'station', stationName: '林间站'};
  assert(controls.stationDwellSeconds(station) === 2, 'default dwell should be 2 seconds');
  controls.cycleStationDwell(station);
  assert(station.stationDwell === 4 && updates === 1 && saves === 1 && message.includes('4秒'), 'station dwell cycle failed');
  station.stationDwell = 8;

  const train = {placed: true, pieceId: 1, entry: 0, exit: 1, t: 0.4, speed: 1, cruiseSpeed: 1, running: true, wait: 0, distance: 0, stationServed: false, powerServed: false, whistleServed: false};
  const motion = context({
    trackPieces: [station],
    trainObstacleDistance: () => null, trainPedestrianDistance: () => null, extraTrainSpeedLimit: () => 1,
    trackPathLength: () => 1, setExtraTrainPose: () => {}, signalIsGreen: () => true,
    stationDwellSeconds: piece => piece.stationDwell || 2,
    triggerDetector: () => {}, connectedTrackPort: () => null,
    trackAllowsEntry: () => true, trackClearanceBlocked: () => false,
    player: {x: 100, z: 100}, tone: () => {}, setTimeout: () => {}
  });
  runFunctions(motion, ['updateExtraTrain']);
  motion.updateExtraTrain(train, 0.2);
  assert(train.t === 0.5 && train.wait === 8 && train.speed === 0 && train.running === true, 'extra train did not honor station dwell');
  assert(SOURCE.includes('const dwell=stationDwellSeconds(piece);trackTrain.wait='), 'primary train dwell hook missing');
  assert(SOURCE.includes("if(lookedTrack?.shape==='station'&&shifted)"), 'Shift+E dwell hook missing');
  assert(SOURCE.includes('data-station-dwell'), 'station panel dwell control missing');
});

test('线路可保存为个人蓝图、持久化、重复摆放与删除', () => {
  let saves = 0;
  let updates = 0;
  let message = '';
  const ctx = context({
    customTrackTemplates: [], trackTemplatePreview: null, line: null,
    trackNetworks: () => [ctx.line], saveGame: () => saves++, updateTrackPanel: () => updates++,
    toast: text => { message = text; }, tone: () => {}, cancelTrackTemplatePreview: () => {}
  });
  loadTrackCatalog(ctx);
  runFunctions(ctx, [
    ...GEOMETRY, 'allTrackTemplates', 'findTrackTemplate', 'stationDwellSeconds', 'trackTemplateSpecFromPiece',
    'saveTrackLineTemplate', 'deleteCustomTrackTemplate', 'loadCustomTrackTemplates'
  ]);
  const pieces = [
    {id: 11, x: 20, y: 4, z: 30, rot: 0, shape: 'straight'},
    {id: 12, x: 20, y: 4, z: 32, rot: 0, shape: 'detector', detectorMode: 3, detectorCount: 99, detectorTargetId: 77},
    {id: 13, x: 20, y: 4, z: 33, rot: 0, shape: 'depot', depotStop: false},
    {id: 14, x: 20, y: 4, z: 35, rot: 0, shape: 'station', stationStop: true, stationDwell: 8, stationName: '夜班站'}
  ];
  ctx.line = {key: 7, name: '山谷线', pieces, minY: 4, maxY: 4};
  ctx.saveTrackLineTemplate(7);
  assert(ctx.customTrackTemplates.length === 1 && saves === 1 && updates === 1, 'custom template was not saved');
  const saved = ctx.customTrackTemplates[0];
  assert(saved.name === '山谷线蓝图' && saved.specs.length === 4 && message.includes('已保存'), 'custom template metadata invalid');
  assert(Math.min(...saved.specs.map(spec => spec.x)) === 0 && Math.min(...saved.specs.map(spec => spec.y)) === 0 && Math.min(...saved.specs.map(spec => spec.z)) === 0, 'custom template was not normalized');
  const sensor = saved.specs.find(spec => spec.shape === 'detector');
  const depot = saved.specs.find(spec => spec.shape === 'depot');
  assert(sensor.detectorMode === 3 && sensor.detectorTargetId === undefined, 'detector blueprint kept runtime target/count');
  const station = saved.specs.find(spec => spec.shape === 'station');
  assert(depot.depotStop === false, 'depot mode was not preserved');
  assert(station.stationDwell === 8 && station.stationName === '夜班站', 'station timetable was not preserved');
  assert(ctx.allTrackTemplates().length === 11 && ctx.findTrackTemplate(saved.id)?.pieces().length === 4, 'custom template is not placeable');

  const serialized = JSON.parse(JSON.stringify(ctx.customTrackTemplates));
  const originalId = saved.id;
  ctx.loadCustomTrackTemplates(serialized);
  assert(ctx.customTrackTemplates.length === 1 && ctx.customTrackTemplates[0].id === originalId, 'custom template persistence changed id');
  ctx.deleteCustomTrackTemplate(originalId);
  assert(ctx.customTrackTemplates.length === 0 && message.includes('已删除'), 'custom template deletion failed');

  ctx.loadCustomTrackTemplates([{id: 'bad', name: 'bad', specs: [{x: 0, y: 0, z: 0, shape: 'not-a-track'}]}]);
  assert(ctx.customTrackTemplates.length === 1 && ctx.customTrackTemplates[0].specs[0].shape === 'straight', 'custom template sanitizer fallback failed');
});

test('轨道模板幽灵预览、旋转、占用迁移、确认与取消', () => {
  const world = new Map();
  const ctx = context({
    W: 128, D: 128, MIN_BUILD_HEIGHT: -10, MAX_BUILD_HEIGHT: 128,
    world, player: {x: 50, y: 1.01, z: 50, yaw: 0}, stock: {37: 1000},
    trackTemplatePreview: null, customTrackTemplates: [], trackPanelReturnToPause: true, selected: 1,
    nextTrackId: 1, trackPieces: [], stats: {placed: 0}, buildHistory: [],
    trackShapeMode: 0, drawbridgeAngles: new Map(),
    key: (x, y, z) => `${x},${y},${z}`,
    get: (x, y, z) => world.get(`${x},${y},${z}`) || 0,
    set: (x, y, z, type) => type ? world.set(`${x},${y},${z}`, type) : world.delete(`${x},${y},${z}`),
    insideWorld: (x, z) => x >= 0 && x < 128 && z >= 0 && z < 128,
    canNetworkEdit: () => true, toast: () => {}, tone: () => {},
    select: value => { ctx.selected = value; }, closeTrackPanel: () => {},
    updateTrackModeHud: () => {}, unlock: () => {}, rebuildMesh: () => {},
    updateInventory: () => {}, drawMinimap: () => {}, saveGame: () => {}, burst: () => {}
  });
  loadTrackCatalog(ctx);
  runFunctions(ctx, [
    ...GEOMETRY, 'trackBaseRotationFromYaw', 'templatePlacementAt', 'locateTrackTemplate',
    'trackTemplatePiecesValid', 'beginTrackTemplatePreview', 'rotateTrackTemplatePreview',
    'cancelTrackTemplatePreview', 'confirmTrackTemplatePreview'
  ]);
  ctx.beginTrackTemplatePreview('block_loop');
  assert(ctx.trackTemplatePreview && ctx.selected === 37, 'preview did not start');
  assert(world.size === 0 && ctx.trackPieces.length === 0, 'preview mutated world');
  const originalTurn = ctx.trackTemplatePreview.turn;
  ctx.rotateTrackTemplatePreview(1);
  assert(ctx.trackTemplatePreview.turn === (originalTurn + 1) % 4, 'preview rotation failed');
  const count = ctx.trackTemplatePreview.pieces.length;
  const before = ctx.stock[37];
  ctx.confirmTrackTemplatePreview();
  assert(!ctx.trackTemplatePreview && ctx.trackPieces.length === count, 'preview confirmation failed');
  assert(ctx.stock[37] === before - count, 'preview inventory accounting failed');
  assert(ctx.buildHistory.at(-1).trackIds.length === count, 'template undo group missing');

  world.clear();
  ctx.trackPieces.length = 0;
  ctx.buildHistory.length = 0;
  ctx.stock[37] = 1000;
  ctx.beginTrackTemplatePreview('tunnel_shuttle');
  const blocked = ctx.trackCells(ctx.trackTemplatePreview.pieces[0])[0];
  ctx.set(blocked.x, blocked.y, blocked.z, 3);
  ctx.confirmTrackTemplatePreview();
  assert(ctx.trackTemplatePreview && ctx.trackPieces.length === 0, 'occupied preview committed immediately');
  assert(ctx.trackTemplatePiecesValid(ctx.trackTemplatePreview.pieces), 'occupied preview did not relocate');
  ctx.confirmTrackTemplatePreview();
  assert(!ctx.trackTemplatePreview && ctx.trackPieces.length === 9, 'relocated preview did not commit');

  world.clear();
  ctx.trackPieces.length = 0;
  ctx.stock[37] = 1000;
  ctx.beginTrackTemplatePreview('yard');
  const cancelStock = ctx.stock[37];
  ctx.cancelTrackTemplatePreview();
  assert(!ctx.trackTemplatePreview && ctx.stock[37] === cancelStock && ctx.trackPieces.length === 0, 'cancel changed world');
});

test('自动闭塞信号检测方向、列车占用与净空', () => {
  const world = new Map();
  const ctx = context({
    trackPieces: [], trackPortIndex: new Map(), trackCellIndex: new Map(), trackColumnIndex: new Map(),
    trackNetworkCache: null, trackTrain: {placed: false, pieceId: null, wagonCount: 0},
    extraTrains: [], drawbridgeAngles: new Map(), world,
    primaryWagonPoses: () => [], trackPieceHasPedestrian: () => false,
    get: (x, y, z) => world.get(`${x},${y},${z}`) || 0,
    key: (x, y, z) => `${x},${y},${z}`,
    isSolidType: type => Boolean(type && type !== 37)
  });
  runFunctions(ctx, [
    'trackDirection', 'rotateTrackOffset', 'trackCells', 'trackCenter', 'trackLocalToWorld',
    'trackPorts', 'trackPortKey', 'rebuildTrackGraph', 'connectedTrackPort', 'trackRouteExit',
    'trackAllowsEntry', 'trackClearanceBlocked', 'trackPointOnPiece', 'trackPieceOccupied',
    'trackSectionStatus', 'autoSignalRouteSafe', 'signalIsGreen', 'signalDisplayGreen', 'signalInfoText'
  ]);
  ctx.trackPieces.push(
    {id: 1, x: 0, y: 1, z: -1, rot: 2, shape: 'stop'},
    {id: 2, x: 0, y: 1, z: 0, rot: 0, shape: 'signal', signalMode: 0},
    {id: 3, x: 0, y: 1, z: 1, rot: 0, shape: 'tunnel'},
    {id: 4, x: 0, y: 1, z: 3, rot: 0, shape: 'signal', signalMode: 0},
    {id: 5, x: 0, y: 1, z: 4, rot: 0, shape: 'stop'}
  );
  ctx.rebuildTrackGraph();
  const signal = ctx.trackPieces[1];
  assert(ctx.signalIsGreen(signal, 0) && ctx.signalIsGreen(signal, 1), 'safe signal should be green');
  ctx.extraTrains.push({placed: true, pieceId: 3});
  assert(!ctx.signalIsGreen(signal, 0) && ctx.signalIsGreen(signal, 1), 'occupied block direction invalid');
  assert(ctx.signalInfoText(signal).includes('区间有列车'), 'occupied reason missing');
  signal.signalMode = 1;
  assert(ctx.signalIsGreen(signal, 0), 'forced green failed');
  signal.signalMode = -1;
  assert(!ctx.signalIsGreen(signal, 1), 'forced red failed');
  signal.signalMode = 0;
  ctx.extraTrains.length = 0;
  world.set('0,2,1', 3);
  assert(!ctx.signalIsGreen(signal, 0) && ctx.signalInfoText(signal).includes('净空不足'), 'clearance signal failed');
});

test('轨道可站立表面：直轨、曲线、坡道、升降桥与空间索引', () => {
  const ctx = context({
    trackPieces: [], trackPortIndex: new Map(), trackCellIndex: new Map(), trackColumnIndex: new Map(),
    trackNetworkCache: null, trackTrain: {placed: false}, drawbridgeAngles: new Map(),
    radius: 0.28, player: {onGround: false},
    key: (x, y, z) => `${x},${y},${z}`,
    collides: () => false
  });
  runFunctions(ctx, [
    'trackDirection', 'turntableSelectedExit', 'rotateTrackOffset', 'trackCells', 'trackCenter', 'trackLocalToWorld',
    'trackPorts', 'trackPortKey', 'rebuildTrackGraph', 'findTrackPiece', 'trackRouteExit',
    'trackPathRaw', 'trackRoutePairs', 'nearbyTrackPieces', 'trackSurfaceHeight', 'trackLandingHeight'
  ]);
  const flat = {id: 1, x: 10, y: 5, z: 10, rot: 0, shape: 'straight'};
  const ramp = {id: 2, x: 20, y: 2, z: 20, rot: 0, shape: 'ramp', rampRise: true};
  const curve = {id: 3, x: 30, y: 7, z: 30, rot: 0, shape: 'curve'};
  const bridge = {id: 4, x: 40, y: 9, z: 40, rot: 0, shape: 'drawbridge', drawbridgeOpen: false};
  const turntable = {id: 5, x: 50, y: 3, z: 50, rot: 0, shape: 'turntable', turntableAxis: 0};
  ctx.trackPieces.push(flat, ramp, curve, bridge, turntable);
  ctx.rebuildTrackGraph();
  const center = ctx.trackCenter(flat);
  near(ctx.trackSurfaceHeight(flat, center.x, center.z), 5.255, 0.001);
  assert(ctx.trackSurfaceHeight(flat, center.x + 0.7, center.z) === null, 'outside rail width is walkable');
  near(ctx.trackLandingHeight(center.x, center.z, 6, 4), 5.255, 0.001);
  assert(ctx.trackLandingHeight(center.x, center.z, 4, 3) === null, 'player landed upward from below');

  for (const piece of [ramp, curve]) {
    const [entry, exit] = ctx.trackRoutePairs(piece)[0];
    for (const u of [0.15, 0.35, 0.6, 0.85]) {
      const point = ctx.trackPathRaw(piece, entry, exit, u);
      near(ctx.trackSurfaceHeight(piece, point.x, point.z), point.y, 0.06, `${piece.shape} surface mismatch`);
    }
  }
  const low = ctx.trackPathRaw(ramp, 0, 1, 0);
  const high = ctx.trackPathRaw(ramp, 0, 1, 1);
  assert(high.y - low.y > 0.95, 'ramp elevation missing');
  const bridgeCenter = ctx.trackCenter(bridge);
  near(ctx.trackSurfaceHeight(bridge, bridgeCenter.x, bridgeCenter.z), 9.255, 0.001);
  bridge.drawbridgeOpen = true;
  assert(ctx.trackSurfaceHeight(bridge, bridgeCenter.x, bridgeCenter.z) === null, 'open bridge remains walkable');
  bridge.drawbridgeOpen = false;
  ctx.drawbridgeAngles.set(bridge.id, 0.3);
  assert(ctx.trackSurfaceHeight(bridge, bridgeCenter.x, bridgeCenter.z) === null, 'moving bridge remains horizontal');
  for (const cell of ctx.trackCells(ramp)) assert(ctx.findTrackPiece(cell.x, cell.y, cell.z) === ramp, 'track cell index failed');
  const turnCenter = ctx.trackCenter(turntable);
  near(ctx.trackSurfaceHeight(turntable, turnCenter.x, turnCenter.z), 3.255, 0.001);
});

test('轨道行人安全检测会让主/附加列车与自动信号停车', () => {
  const ctx = context({
    trackPieces: [], trackPortIndex: new Map(), trackCellIndex: new Map(), trackColumnIndex: new Map(),
    trackNetworkCache: null, trackTrain: {placed: false}, extraTrains: [], drawbridgeAngles: new Map(),
    ridingTrain: false, flying: false, player: {x: 0.5, y: 1.255, z: 1.5}, remotes: [],
    activeRemotePlayers: () => ctx.remotes,
    primaryWagonPoses: () => [],
    key: (x, y, z) => `${x},${y},${z}`
  });
  runFunctions(ctx, [
    'trackDirection', 'turntableSelectedExit', 'rotateTrackOffset', 'trackCells', 'trackCenter',
    'trackLocalToWorld', 'trackPorts', 'trackPortKey', 'rebuildTrackGraph', 'connectedTrackPort',
    'trackRouteExit', 'trackAllowsEntry', 'trackPathRaw', 'trackPathLength', 'trackPointOnPiece',
    'trackPedestrians', 'trackPieceHasPedestrian', 'trackPathPedestrianDistance', 'trainPedestrianDistance'
  ]);
  const straight = {id: 1, x: 0, y: 1, z: 0, rot: 0, shape: 'straight'};
  ctx.trackPieces.push(straight);
  ctx.rebuildTrackGraph();
  const train = {placed: true, pieceId: 1, entry: 0, exit: 1, t: 0.2};
  const ahead = ctx.trainPedestrianDistance(train, straight);
  assert(ahead !== null && ahead > 0.8 && ahead < 1.3, `ahead pedestrian distance invalid: ${ahead}`);
  assert(ctx.trackPieceHasPedestrian(straight), 'pedestrian was not recognized on rail');

  ctx.player.z = -0.4;
  assert(ctx.trainPedestrianDistance(train, straight) === null, 'pedestrian behind train caused braking');
  ctx.player.z = 1.5;
  ctx.player.y = 4;
  assert(ctx.trainPedestrianDistance(train, straight) === null, 'high pedestrian caused braking');
  ctx.player.y = 1.255;
  ctx.ridingTrain = true;
  assert(ctx.trainPedestrianDistance(train, straight) === null, 'local rider was treated as pedestrian');
  ctx.ridingTrain = false;
  ctx.flying = true;
  ctx.remotes = [{x: 0.5, y: 1.255, z: 1.4, flying: false, ridingTrain: false}];
  assert(ctx.trainPedestrianDistance(train, straight) !== null, 'remote pedestrian was ignored');
  ctx.remotes[0].ridingTrain = true;
  assert(ctx.trainPedestrianDistance(train, straight) === null, 'remote rider was treated as pedestrian');

  assert(SOURCE.includes('const pedestrian=trainPedestrianDistance(trackTrain,piece)'), 'primary train pedestrian braking missing');
  assert(SOURCE.includes('const pedestrian=trainPedestrianDistance(train,piece)'), 'extra train pedestrian braking missing');
  assert(SOURCE.includes("reason:'轨道上有人'"), 'automatic signal pedestrian reason missing');
  assert(SOURCE.includes('drawTrainSafetyWarnings()'), 'pedestrian warning light missing');
  assert(SOURCE.includes('r.ridingTrain=!!msg.ridingTrain'), 'remote rider state missing');
});

test('轨道快捷模式覆盖20种图标与数字映射，并返回原积木', () => {
  const palette = {hidden: false, innerHTML: '', classList: {toggle(name, value) { if (name === 'hidden') palette.hidden = value; }}};
  const ctx = context({
    trackKeyboardMode: false, selected: 2, lastNonTrackSelected: 2, trackTemplatePreview: null,
    trackShapeMode: 0, saves: 0,
    document: {getElementById: id => { assert(id === 'trackKeyboardPalette'); return palette; }},
    stopMining: () => {}, stopPlacing: () => {}, updateTrackModeHud: () => {}, toast: () => {},
    saveGame: () => { ctx.saves++; }
  });
  loadTrackCatalog(ctx);
  ctx.select = value => {
    ctx.selected = value;
    if (value !== 37) {
      ctx.lastNonTrackSelected = value;
      ctx.trackKeyboardMode = false;
    }
  };
  runFunctions(ctx, ['trackShortcutLabel', 'trackIconSvg', 'updateTrackKeyboardPalette', 'chooseTrackShortcut', 'toggleTrackKeyboardMode']);
  ctx.toggleTrackKeyboardMode();
  assert(ctx.trackKeyboardMode && ctx.selected === 37, 'keyboard mode did not start');
  for (let digit = 1; digit <= 9; digit++) {
    assert(ctx.chooseTrackShortcut(`Digit${digit}`, false), `Digit${digit} rejected`);
    assert(ctx.trackShapeMode === digit - 1, `Digit${digit} mapping invalid`);
  }
  assert(ctx.chooseTrackShortcut('Digit0', false) && ctx.trackShapeMode === 9, 'Digit0 mapping invalid');
  for (let digit = 1; digit <= 9; digit++) {
    assert(ctx.chooseTrackShortcut(`Digit${digit}`, true), `Shift+${digit} rejected`);
    assert(ctx.trackShapeMode === 9 + digit, `Shift+${digit} mapping invalid`);
  }
  assert(ctx.chooseTrackShortcut('Digit0', true) && ctx.trackShapeMode === 19 && ctx.trackShortcutLabel(19) === '⇧0', 'Shift+0 depot mapping invalid');
  ctx.updateTrackKeyboardPalette();
  assert(!palette.hidden, 'palette hidden in keyboard mode');
  assert((palette.innerHTML.match(/data-track-key-index=/g) || []).length === 20, 'palette item count invalid');
  assert(palette.innerHTML.includes('旋转轨') && palette.innerHTML.includes('感应轨') && palette.innerHTML.includes('木质车库') && palette.innerHTML.includes('⇧0'), 'new rail absent from palette');
  assert(new Set(Array.from(ctx.TRACK_SHAPES, ctx.trackIconSvg)).size === 20, 'icons are not unique');
  ctx.toggleTrackKeyboardMode();
  assert(!ctx.trackKeyboardMode && ctx.selected === 2, 'keyboard mode did not restore previous block');
});

test('道口栏杆阻挡道路而不是火车', () => {
  assert(SOURCE.includes('post(-.53,-.55);post(.53,.55)'), 'crossing posts changed unexpectedly');
  const arms = [[-0.53, -0.55, 0], [0.53, 0.55, Math.PI]];
  for (const [baseX, baseZ, yaw] of arms) {
    const points = [0, 1].map(z => [baseX - z * Math.sin(yaw), baseZ + z * Math.cos(yaw)]);
    const dx = Math.abs(points[1][0] - points[0][0]);
    const dz = Math.abs(points[1][1] - points[0][1]);
    assert(dz > 0.95 && dx < 1e-6, 'gate is not parallel to track');
    assert(Math.min(...points.map(point => Math.abs(point[0]))) > 0.45, 'gate blocks train envelope');
    assert(Math.min(...points.map(point => point[1])) < -0.34 && Math.max(...points.map(point => point[1])) > 0.34, 'gate does not span road lanes');
  }
});

test('水、玻璃与轨道接触面规则保持无闪烁', () => {
  const ctx = context();
  runFunctions(ctx, ['occludesCubeFace', 'hidesWaterContactFace', 'hidesGlassContactFace']);
  assert(!ctx.occludesCubeFace(37), 'track incorrectly hides neighbor face');
  assert(!ctx.occludesCubeFace(18) && !ctx.occludesCubeFace(34), 'transparent block occlusion regressed');
  assert(!ctx.hidesWaterContactFace(37), 'track incorrectly hides water contact');
  assert(ctx.hidesWaterContactFace(34), 'glass should hide internal water contact');
  assert(ctx.hidesGlassContactFace(34) && ctx.hidesGlassContactFace(3), 'glass contact hiding regressed');
  assert(!ctx.hidesGlassContactFace(37), 'track incorrectly hides glass contact');
});

test('备份JSON校验接受有效存档并拒绝损坏数据', () => {
  const ctx = context();
  runFunctions(ctx, ['validateBackupText']);
  const valid = JSON.stringify({v: 3, world: [['1,0,1', 1]], marker: 'ok'});
  assert(ctx.validateBackupText(valid).marker === 'ok', 'valid backup rejected');
  for (const bad of [
    '{not-json',
    JSON.stringify({v: 2, world: []}),
    JSON.stringify({v: 3, world: [['bad']]}),
    JSON.stringify({v: 3, world: [[4, 1]]})
  ]) {
    let rejected = false;
    try { ctx.validateBackupText(bad); } catch (_) { rejected = true; }
    assert(rejected, `invalid backup accepted: ${bad.slice(0, 30)}`);
  }
});

console.log(`\n客户端回归测试完成：${passed} 组全部通过。`);
