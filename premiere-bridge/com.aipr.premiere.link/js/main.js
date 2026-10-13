/*
 * aiPr Link —— CEP 面板的桥接逻辑。
 *
 * 协议（与 pymiere Python 侧约定）：
 *   GET  /            -> 纯文本 "Premiere is alive"（Python 用它探活）
 *   POST /  {to_eval} -> 在 Premiere 里执行 ExtendScript 并把返回值原样回传
 *
 * Python 侧会把代码里的反斜杠转义成 "\\"，因为它发的是「源码文本」，
 * 所以这里必须原样交给 __adobe_cep__.evalScript，不要再转义一次。
 */

(function () {
  'use strict'

  var HOST = '127.0.0.1'
  // 允许自测脚本换端口（scripts/selftest_bridge.mjs），正常使用固定 3000
  var PORT = window.__aiprBridgePort || 3000
  var MAX_BODY_BYTES = 4 * 1024 * 1024
  var MAX_LOG_LINES = 40

  var elements = {
    dot: document.getElementById('dot'),
    status: document.getElementById('status'),
    endpoint: document.getElementById('endpoint'),
    count: document.getElementById('count'),
    log: document.getElementById('log'),
  }

  var handledRequests = 0

  function setStatus(text, ok) {
    if (elements.status) {
      elements.status.textContent = text
    }
    if (elements.dot) {
      elements.dot.className = 'dot' + (ok === true ? ' ok' : ok === false ? ' err' : '')
    }
  }

  function appendLog(text, isError) {
    if (!elements.log) {
      return
    }
    var line = document.createElement('div')
    var stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    line.textContent = stamp + ' ' + text
    if (isError) {
      line.className = 'error'
    }
    elements.log.appendChild(line)
    while (elements.log.childNodes.length > MAX_LOG_LINES) {
      elements.log.removeChild(elements.log.firstChild)
    }
    elements.log.scrollTop = elements.log.scrollHeight
  }

  function bumpCounter() {
    handledRequests += 1
    if (elements.count) {
      elements.count.textContent = String(handledRequests)
    }
  }

  function isExtendScriptAvailable() {
    return !!(window.__adobe_cep__ && window.__adobe_cep__.evalScript)
  }

  /** 把一段源码交给 Premiere 的 ExtendScript 引擎执行。 */
  function evalScript(code, callback) {
    if (!isExtendScriptAvailable()) {
      callback(
        '{"error":true,"name":"CEPUnavailable","line":1,"source":"",' +
          '"message":"面板没有运行在 Premiere 内（window.__adobe_cep__ 不存在），请在 Premiere 的【窗口 > 扩展 > aiPr Link】中打开本面板"}',
      )
      return
    }
    try {
      window.__adobe_cep__.evalScript(code, callback)
    } catch (error) {
      callback(
        '{"error":true,"name":"EvalScriptFailed","line":1,"source":"",' +
          '"message":"' + String(error && error.message ? error.message : error).replace(/"/g, "'") + '"}',
      )
    }
  }

  /**
   * 执行 Python 发来的代码。
   *
   * 两处兼容处理：
   * 1. pymiere 的 core._format_object_to_es() 把字符串字面量格式化成 """text"""，
   *    ExtendScript 不认这种写法（等价于 "" "text" ""），这里还原成标准双引号字符串；
   * 2. 前面拼一句 _lastCode 记录本次源码：ExtendScript 抛错时运行时会把这段源码
   *    作为 error.source 回传，Python 侧就能打印出错的那一行。
   */
  function runCode(code) {
    var normalized = code.replace(/"""(.*?)"""/g, '"$1"')
    var sourceRecorder = '$._pymiere._lastCode=' + JSON.stringify(normalized) + ';'
    return sourceRecorder + normalized
  }

  function sendText(response, statusCode, text) {
    response.writeHead(statusCode, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
      'Access-Control-Allow-Origin': '*',
    })
    response.end(text)
  }

  function handlePost(request, response) {
    var chunks = []
    var size = 0

    request.on('data', function (chunk) {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        request.destroy()
        sendText(response, 413, '请求体过大')
        return
      }
      chunks.push(chunk)
    })

    request.on('end', function () {
      if (size > MAX_BODY_BYTES) {
        return
      }
      var payload
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      } catch (error) {
        sendText(response, 400, '请求体不是合法 JSON')
        return
      }

      var code = payload && payload.to_eval
      if (typeof code !== 'string' || !code.length) {
        sendText(response, 400, '缺少 to_eval 字段')
        return
      }

      bumpCounter()
      evalScript(runCode(code), function (result) {
        var text = result === null || result === undefined ? 'undefined' : String(result)
        var preview = code.replace(/\s+/g, ' ').slice(0, 90)
        var failed = text.indexOf('"error":true') !== -1
        appendLog((failed ? '执行出错：' : '执行：') + preview, failed)
        sendText(response, 200, text)
      })
    })

    request.on('error', function () {
      sendText(response, 500, '读取请求失败')
    })
  }

  function startServer() {
    var http
    try {
      http = require('http')
    } catch (error) {
      setStatus('Node 未启用：请在清单里确认 --enable-nodejs', false)
      appendLog('require("http") 失败：' + error.message, true)
      return
    }

    var server = http.createServer(function (request, response) {
      if (request.method === 'GET') {
        sendText(response, 200, 'Premiere is alive')
        return
      }
      if (request.method === 'POST') {
        handlePost(request, response)
        return
      }
      sendText(response, 405, '只支持 GET / POST')
    })

    server.on('error', function (error) {
      if (error.code === 'EADDRINUSE') {
        setStatus('端口 ' + PORT + ' 已被占用（可能面板开了两次）', false)
      } else {
        setStatus('本地服务启动失败：' + error.message, false)
      }
      appendLog('服务错误：' + error.message, true)
    })

    server.listen(PORT, HOST, function () {
      setStatus('已就绪，等待 Python 调用', true)
      appendLog('正在监听 http://' + HOST + ':' + PORT)
      if (elements.endpoint) {
        elements.endpoint.textContent = HOST + ':' + PORT
      }
    })
  }

  if (!isExtendScriptAvailable()) {
    setStatus('未检测到 CEP 环境，请在 Premiere 内打开本面板', false)
  }

  startServer()
})()
