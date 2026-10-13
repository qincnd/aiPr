/*
 * aiPr Link 桥接自测（Node）。
 *
 * 目的：在没有 Premiere 的情况下验证两件事
 *   1. 面板的 HTTP 协议 + ExtendScript 运行时（ExtendJSON / $._pymiere / 错误序列化）是否正确；
 *   2. 我们的 pymiere 适配器在「真实类型约定」下能不能跑通：
 *      Clip 的 start/end/inPoint/outPoint 都是 Time 对象、importFiles 有严格类型检查、
 *      集合用 numItems/numTracks/numSequences 计数……
 *
 * 做法：造一个假的 CEP 宿主（window / document / __adobe_cep__）+ 一个 Premiere 对象模型仿真器，
 *       在里面加载【真实】的 js/main.js 与 jsx/runtime.jsx，再由 Python 侧用 pymiere 真身调用。
 *
 * 用法：node scripts/selftest_bridge.mjs [端口]
 */

import { createRequire } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const panelDir = join(here, '..', 'premiere-bridge', 'com.aipr.premiere.link')
const port = Number(process.argv[2] || 3100)

// 有真机时把 app.path 指向真实安装目录，这样序列预设解析也能被一起验证
const REAL_PREMIERE_DIR = 'D:\\Adobe Premiere Pro 2026\\Adobe Premiere Pro 2026'
const appPath = existsSync(REAL_PREMIERE_DIR) ? REAL_PREMIERE_DIR : process.cwd()

// ---------------------------------------------------------------- Premiere 时间对象
function Time(seconds = 0) {
  this.reflect = { name: 'Time' }
  this.seconds = seconds
}

Time.prototype.getFormatted = function getFormatted(frameRate) {
  const frameTime = frameRate && frameRate.seconds ? frameRate.seconds : 1 / 30
  const fps = Math.max(1, Math.round(1 / frameTime))
  const totalFrames = Math.round(this.seconds * fps)
  const frames = totalFrames % fps
  const totalSeconds = Math.floor(totalFrames / fps)
  const pad = (value) => String(value).padStart(2, '0')
  return `${pad(Math.floor(totalSeconds / 3600))}:${pad(Math.floor(totalSeconds / 60) % 60)}:${pad(
    totalSeconds % 60,
  )}:${pad(frames)}`
}

// ---------------------------------------------------------------- 集合（每次读取都重建快照）
const COUNT_PROPERTIES = {
  TrackCollection: 'numTracks',
  SequenceCollection: 'numSequences',
  ProjectItemCollection: 'numItems',
  TrackItemCollection: 'numItems',
  ComponentCollection: 'numItems',
  ComponentParamCollection: 'numItems',
  MarkerCollection: 'numMarkers',
}

function makeCollection(typeName, items) {
  const collection = { reflect: { name: typeName } }
  collection[COUNT_PROPERTIES[typeName] || 'numItems'] = items.length
  items.forEach((item, index) => {
    collection[index] = item
  })
  return collection
}

function defineGetter(target, property, producer) {
  Object.defineProperty(target, property, { get: producer, enumerable: true, configurable: true })
}

// ---------------------------------------------------------------- 对象模型
function clipDuration(clip) {
  return Math.max(0, clip.end.seconds - clip.start.seconds)
}

class TrackItem {
  constructor(projectItem, startSeconds) {
    this.reflect = { name: 'TrackItem' }
    this.name = projectItem.name
    this.projectItem = projectItem
    this.start = new Time(startSeconds)
    this.end = new Time(startSeconds + projectItem.durationSeconds)
    this.inPoint = new Time(0)
    this.outPoint = new Time(projectItem.durationSeconds)
    this.removed = false

    const levelParam = {
      reflect: { name: 'ComponentParam' },
      displayName: 'Level',
      value: 0,
      setValue(value) {
        this.value = value
      },
      getValue() {
        return this.value
      },
    }
    const volumeComponent = {
      reflect: { name: 'Component' },
      displayName: 'Volume',
      properties: makeCollection('ComponentParamCollection', [levelParam]),
    }
    this.effects = []
    defineGetter(this, 'components', () =>
      makeCollection('ComponentCollection', [
        volumeComponent,
        // 效果（addVideoEffect 之后真的多出一个组件，和真机一致）
        ...this.effects.map((name) => ({
          reflect: { name: 'Component' },
          displayName: name,
          properties: makeCollection('ComponentParamCollection', []),
        })),
      ]),
    )
  }

  remove() {
    this.removed = true
    return true
  }

  isMGT() {
    return false
  }

  getSpeed() {
    return 1
  }
}

class TrackTransition {
  constructor(name, startSeconds, durationSeconds) {
    this.reflect = { name: 'Component' }
    this.name = name
    this.start = new Time(startSeconds)
    this.end = new Time(startSeconds + durationSeconds)
    this.duration = new Time(durationSeconds)
  }
}

class Track {
  constructor(name, mediaType) {
    this.reflect = { name: 'Track' }
    this.name = name
    this.mediaType = mediaType
    this.items = []
    this.transitionList = []
    defineGetter(this, 'clips', () =>
      makeCollection('TrackItemCollection', this.items.filter((item) => !item.removed)),
    )
    // 真机上转场挂在轨道上（TrackItem 上没有 transitions），这里照真机建模
    defineGetter(this, 'transitions', () =>
      makeCollection('TrackItemCollection', this.transitionList),
    )
  }

  insertClip(projectItem, time) {
    const start = time && typeof time.seconds === 'number' ? time.seconds : Number(time) || 0
    const clip = new TrackItem(projectItem, start)
    this.items.push(clip)
    return clip
  }

  overwriteClip(projectItem, time) {
    return this.insertClip(projectItem, time)
  }
}

class Marker {
  constructor(seconds) {
    this.reflect = { name: 'Marker' }
    this.name = 'marker'
    this.comments = ''
    this.start = new Time(seconds)
    this.end = new Time(seconds)
  }
}

class Sequence {
  constructor(name) {
    this.reflect = { name: 'Sequence' }
    this.name = name
    this.video = [new Track('V1', 1), new Track('V2', 1)]
    this.audio = [new Track('A1', 2), new Track('A2', 2)]
    this.markerList = []
    defineGetter(this, 'videoTracks', () => makeCollection('TrackCollection', this.video))
    defineGetter(this, 'audioTracks', () => makeCollection('TrackCollection', this.audio))
    defineGetter(this, 'markers', () => {
      const collection = makeCollection('MarkerCollection', this.markerList)
      collection.createMarker = (seconds) => {
        const marker = new Marker(seconds)
        this.markerList.push(marker)
        return marker
      }
      collection.getFirstMarker = () => this.markerList[0] || null
      collection.getLastMarker = () => this.markerList[this.markerList.length - 1] || null
      return collection
    })
  }

  getSettings() {
    return {
      reflect: { name: 'SequenceSettings' },
      videoFrameRate: new Time(1 / 30),
      videoFrameWidth: 1920,
      videoFrameHeight: 1080,
      videoPixelAspectRatio: 1,
      videoDisplayFormat: 0,
      audioSampleRate: 48000,
    }
  }
}

class ProjectItem {
  constructor(name, mediaPath, durationSeconds = 4) {
    this.reflect = { name: 'ProjectItem' }
    this.name = name
    this.mediaPath = mediaPath
    this.type = mediaPath ? 1 : 2
    this.durationSeconds = durationSeconds
    this.childItems = []
    defineGetter(this, 'children', () =>
      mediaPath ? null : makeCollection('ProjectItemCollection', this.childItems),
    )
  }

  getMediaPath() {
    return this.mediaPath
  }

  // 真机上 bin 只能由 ProjectItem（根素材箱）创建
  createBin(name) {
    const bin = new ProjectItem(name, null)
    this.childItems.push(bin)
    return bin
  }

  getFootageInterpretation() {
    return { reflect: { name: 'FootageInterpretation' }, frameRate: 30 }
  }
}

const rootItem = new ProjectItem('root', null)
rootItem.type = 3
rootItem.childItems.push(
  new ProjectItem('premiere_shot.mp4', 'E:\\media\\premiere_shot.mp4', 6),
  new ProjectItem('premiere_bgm.wav', 'E:\\media\\premiere_bgm.wav', 30),
)

const sequences = []
const encoderLog = []

const project = {
  reflect: { name: 'Project' },
  name: '仿真工程',
  path: 'E:\\projects\\selftest.prproj',
  rootItem,
  get sequences() {
    return makeCollection('SequenceCollection', sequences)
  },
  get activeSequence() {
    return sequences.length ? sequences[sequences.length - 1] : null
  },
  importFiles(paths, suppressUI, targetBin) {
    const bin = targetBin || rootItem
    return paths.map((path) => {
      const name = String(path).split(/[\\/]/).pop()
      const item = new ProjectItem(name, String(path), 5)
      bin.childItems.push(item)
      return item
    })
  },
  createNewSequenceFromClips(name, items) {
    const sequence = new Sequence(name)
    let cursor = 0
    items.forEach((item) => {
      const clip = sequence.video[0].insertClip(item, new Time(cursor))
      cursor += clipDuration(clip)
    })
    sequences.push(sequence)
    return sequence
  },
  createNewSequence(name) {
    sequences.push(new Sequence(name))
    return null
  },
  save() {
    return true
  },
  get encoder() {
    return {
      reflect: { name: 'Encoder' },
      ENCODE_ENTIRE: 0,
      ENCODE_IN_TO_OUT: 1,
      ENCODE_WORKAREA: 2,
      encodeSequence(sequence, output, preset, workArea, remove) {
        encoderLog.push({ sequence: sequence.name, output, preset, workArea, remove })
        return true
      },
    }
  },
}

const app = {
  reflect: { name: 'Application' },
  version: '26.0.0',
  path: appPath,
  project,
  getCurrentProject: () => project,
  openDocument: () => true,
  isDocumentOpen: () => true,
}

// QE 仿真：真机（PR 2026）的 QE 轨道要从 getActiveSequence() 上取，
// item 是 Clip/Empty 交替的（第 k 个片段在索引 2k，所以序号定位不可靠），
// addTransition 只认 3 个参数且对齐被选中片段的出点，razor 挂在轨道上。
function qeTrack(sequence, kind, index) {
  const track = kind === 'video' ? sequence.video[index] : sequence.audio[index]
  const items = () =>
    track.items.filter((item) => !item.removed).sort((a, b) => a.start.seconds - b.start.seconds)
  // 真机的 QE item 列表里片段之间夹着 Empty 占位
  const qeItems = () => {
    const result = []
    items().forEach((item) => {
      result.push({ name: item.name, item })
      result.push({ name: '', item: null })
    })
    return result
  }
  const trackProxy = {
    reflect: { name: 'Track' },
    get numItems() {
      return qeItems().length
    },
    razor(timecode) {
      const seconds = timecodeToSeconds(timecode)
      const target = items().find(
        (item) => seconds > item.start.seconds && seconds < item.end.seconds,
      )
      if (!target) {
        return true
      }
      const outPoint = target.outPoint.seconds
      const tailStart = seconds
      const tail = new TrackItem(target.projectItem, tailStart)
      tail.inPoint = new Time(target.inPoint.seconds + (seconds - target.start.seconds))
      tail.outPoint = new Time(outPoint)
      tail.end = new Time(tailStart + (outPoint - tail.inPoint.seconds))
      target.end = new Time(seconds)
      target.outPoint = new Time(tail.inPoint.seconds)
      track.items.push(tail)
      return true
    },
    getItemAt(position) {
      const entry = qeItems()[position]
      if (!entry) {
        throw new Error('QE: no item at ' + position)
      }
      return {
        reflect: { name: 'TrackItem' },
        name: entry.name,
        addTransition(transition, forceSingle, duration, extra) {
          if (extra !== undefined) {
            throw new Error('Illegal Parameter type')
          }
          const target = entry.item
          if (!target || !transition || !transition.name) {
            // 真机上给 Empty 位或无效转场对象加转场是静默失效
            return true
          }
          track.transitionList.push(
            // 真机的转场对齐「被选中片段的出点」
            new TrackTransition(
              transition.name,
              target.end.seconds - resolveTransitionSeconds(duration),
              resolveTransitionSeconds(duration),
            ),
          )
          return true
        },
        addVideoEffect(effect, extra) {
          if (extra !== undefined) {
            throw new Error('Illegal Parameter type')
          }
          const target = entry.item
          if (!target || !effect || !effect.name) {
            return true
          }
          target.effects.push(effect.name)
          return true
        },
      }
    },
  }
  return trackProxy
}

function timecodeToSeconds(timecode) {
  const match = /^(\d+):(\d+):(\d+):(\d+)$/.exec(String(timecode))
  if (!match) {
    return Number(timecode) || 0
  }
  const [, hours, minutes, seconds, frames] = match.map(Number)
  return hours * 3600 + minutes * 60 + seconds + frames / 30
}

// QE 的 addTransition 签名在不同版本上不一样，仿真器只认得「时间码/秒数」这一类参数
function resolveTransitionSeconds(...candidates) {
  for (const candidate of candidates) {
    if (candidate === undefined || candidate === null || typeof candidate === 'boolean') {
      continue
    }
    const seconds = timecodeToSeconds(candidate)
    if (seconds > 0) {
      return seconds
    }
  }
  return 1
}

// 真机上这两个 lookup 对不存在的名字不报错，只返回 name 为空的对象；
// 效果名在中文版里是本地化的（"Lumetri 颜色"），转场则仍认英文内部名（"Cross Dissolve"）。
const FAKE_EFFECTS = ['Lumetri 颜色', 'Gaussian Blur', '裁剪', '黑白']
const FAKE_TRANSITIONS = ['交叉溶解（旧版）', 'Cross Dissolve', 'Dip to Black']

const qe = {
  reflect: { name: 'QE' },
  project: {
    // PR 2026 的 qe.project 上其实没有 getVideoTrackAt，写在这里只是为了暴露误用
    getVideoTrackAt: () => {
      throw new Error('qe.project.getVideoTrackAt 在 PR 2026 上不存在')
    },
    getActiveSequence: () => {
      const sequence = project.activeSequence
      return {
        reflect: { name: 'Sequence' },
        getVideoTrackAt: (index) => qeTrack(sequence, 'video', index),
        getAudioTrackAt: (index) => qeTrack(sequence, 'audio', index),
        // 整条时间线切割：PR 2026 上 QE 的 sequence 直接带 razor
        razor: (timecode) => {
          sequence.video.forEach((_, index) => qeTrack(sequence, 'video', index).razor(timecode))
          sequence.audio.forEach((_, index) => qeTrack(sequence, 'audio', index).razor(timecode))
          return true
        },
      }
    },
    getVideoEffectList: () => FAKE_EFFECTS.slice(),
    getVideoTransitionList: () => FAKE_TRANSITIONS.slice(),
    getVideoTransitionByName: (name) => ({
      reflect: { name: 'VideoTransition' },
      name: FAKE_TRANSITIONS.indexOf(String(name)) >= 0 ? String(name) : '',
    }),
    getVideoEffectByName: (name) => ({
      reflect: { name: 'VideoEffect' },
      name: FAKE_EFFECTS.indexOf(String(name)) >= 0 ? String(name) : '',
    }),
  },
}

// ---------------------------------------------------------------- 最小 DOM 桩
function makeElement(id) {
  const element = {
    id,
    textContent: '',
    className: '',
    childNodes: [],
    scrollTop: 0,
    scrollHeight: 0,
    get firstChild() {
      return element.childNodes[0]
    },
    appendChild(child) {
      element.childNodes.push(child)
      return child
    },
    removeChild(child) {
      const index = element.childNodes.indexOf(child)
      if (index >= 0) {
        element.childNodes.splice(index, 1)
      }
      return child
    },
  }
  return element
}

const elements = new Map()
const documentStub = {
  getElementById(id) {
    if (!elements.has(id)) {
      elements.set(id, makeElement(id))
    }
    return elements.get(id)
  },
  createElement: (tag) => makeElement(tag),
}

// ---------------------------------------------------------------- 假 CEP 宿主
const sandbox = {
  console,
  Buffer,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  require: createRequire(import.meta.url),
  process,
  JSON,
  Math,
  Date,
  Array,
  Object,
  String,
  Number,
  Boolean,
  Error,
  isFinite,
  parseFloat,
  parseInt,
  document: documentStub,
  app,
  qe,
  Time,
  $: {},
}

sandbox.window = sandbox
sandbox.global = sandbox

const context = vm.createContext(sandbox)

// 与 CEP 的 ScriptPath 行为一致：面板加载时先注入运行时
vm.runInContext(readFileSync(join(panelDir, 'jsx', 'runtime.jsx'), 'utf8'), context, {
  filename: 'runtime.jsx',
})

sandbox.window.__adobe_cep__ = {
  evalScript(code, callback) {
    try {
      const result = vm.runInContext(code, context, { filename: 'extendscript-call.jsx', timeout: 10000 })
      callback(result === undefined ? 'undefined' : String(result))
    } catch (error) {
      callback(
        JSON.stringify({
          error: true,
          name: 'HostError',
          message: String((error && error.message) || error),
          line: 1,
          source: code,
        }),
      )
    }
  },
}

sandbox.window.__aiprBridgePort = port

vm.runInContext(readFileSync(join(panelDir, 'js', 'main.js'), 'utf8'), context, {
  filename: 'main.js',
})

console.log(`[selftest] 假 CEP 宿主已启动，面板代码监听 127.0.0.1:${port}`)
console.log(`[selftest] 仿真器 app.path = ${appPath}`)
console.log(
  `[selftest] 真实预设目录可用 = ${existsSync(join(appPath, 'Settings', 'SequencePresets'))}`,
)
