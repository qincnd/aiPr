<#
.SYNOPSIS
    安装 / 卸载 aiPr Link（驱动 Premiere Pro 的 CEP 面板）。

.DESCRIPTION
    做两件事：
      1. 把 premiere-bridge\com.aipr.premiere.link 复制到 %APPDATA%\Adobe\CEP\extensions\；
      2. 打开 PlayerDebugMode，让未签名的扩展可以被 Premiere 加载。

    只写 HKCU，不需要管理员权限。

.PARAMETER Uninstall
    卸载扩展，并把 PlayerDebugMode 关掉。

.PARAMETER Check
    安装后顺便探测 127.0.0.1:3000（需要 Premiere 已打开且面板已加载）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-premiere-bridge.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

$bundleId = 'com.aipr.premiere.link'
$source = Join-Path $PSScriptRoot "..\premiere-bridge\$bundleId"
$source = (Resolve-Path $source).Path
$target = Join-Path $env:APPDATA "Adobe\CEP\extensions\$bundleId"
$csxsVersions = 9..14
$panelUrl = 'http://127.0.0.1:3000'

function Set-PlayerDebugMode([bool]$enabled) {
    foreach ($version in $csxsVersions) {
        $path = "HKCU:\Software\Adobe\CSXS.$version"
        if ($enabled) {
            if (-not (Test-Path $path)) {
                New-Item -Path $path -Force | Out-Null
            }
            New-ItemProperty -Path $path -Name 'PlayerDebugMode' -Value '1' -PropertyType String -Force | Out-Null
        } elseif (Test-Path $path) {
            Remove-ItemProperty -Path $path -Name 'PlayerDebugMode' -ErrorAction SilentlyContinue
        }
    }
}

if ($Uninstall) {
    if (Test-Path $target) {
        Remove-Item -Recurse -Force $target
        Write-Host "已删除扩展：$target"
    }
    Set-PlayerDebugMode $false
    Write-Host '已关闭 PlayerDebugMode。'
    return
}

if (-not (Test-Path $source)) {
    throw "找不到面板源码：$source"
}

$parent = Split-Path $target -Parent
if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

if (Test-Path $target) {
    Remove-Item -Recurse -Force $target
}
Copy-Item -Recurse -Path $source -Destination $target
Write-Host "已安装扩展：$target"

Set-PlayerDebugMode $true
Write-Host '已打开 PlayerDebugMode（CSXS.9 ~ CSXS.14）。'

Write-Host ''
Write-Host '接下来：' -ForegroundColor Cyan
Write-Host '  1. 重启 Premiere Pro，打开【窗口 > 扩展 > aiPr Link】'
Write-Host '  2. 保持面板打开，然后运行：python scripts\check_premiere.py'

if ($Check) {
    Write-Host ''
    Write-Host "探测 $panelUrl ..." -ForegroundColor Cyan
    try {
        $response = Invoke-WebRequest -Uri $panelUrl -TimeoutSec 5 -UseBasicParsing
        if ($response.Content -eq 'Premiere is alive') {
            Write-Host '桥接已就绪：面板正在监听。' -ForegroundColor Green
        } else {
            Write-Host "端口有响应但内容不对：$($response.Content)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "暂时连不上（Premiere 未启动或面板未打开）：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}
