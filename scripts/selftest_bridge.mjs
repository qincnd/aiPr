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
    defineGetter(this, 'components', () => makeCollection('ComponentCollection', [volumeComponent]))
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

class Track {
  constructor(name, mediaType) {
    this.reflect = { name: 'Track' }
    this.name = name
    this.mediaType = mediaType
    this.items = []
    defineGetter(this, 'clips', () =>
      makeCollection('TrackItemCollection', this.items.filter((item) => !item.removed)),
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

const qe = {
  reflect: { name: 'QE' },
  project: {
    getVideoTrackAt: () => ({ razor: () => true, addTransition: () => true, numItems: 0 }),
    getAudioTrackAt: () => ({ razor: () => true, numItems: 0 }),
    getVideoTransitionByName: () => ({ reflect: { name: 'VideoTransition' } }),
    getVideoEffectByName: () => ({ reflect: { name: 'VideoEffect' } }),
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
