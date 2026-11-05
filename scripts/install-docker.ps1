# Docker 安装向导（Windows 11 Pro + WSL2 后端）
#
#   ## 必须以管理员身份运行 ##
#   开始菜单搜 PowerShell -> 右键「以管理员身份运行」-> 然后：
#   cd "D:\DSH proj\AI记忆"
#   powershell -ExecutionPolicy Bypass -File scripts/install-docker.ps1
#
# 为什么不直接用 `wsl --install`：它走在线/商店安装路径，本机会报「已禁止(403)」。
# 这个脚本改走本地组件库（DISM）+ 离线内核 MSI，完全绕开那条链路。
#
# 脚本是**分阶段幂等**的：每次运行先探测当前状态，只做还缺的那一步。
#   阶段 1  启用 WSL / 虚拟机平台可选功能            -> 需要重启
#   阶段 2  安装 WSL2 内核 MSI                        -> 可能需要重启
#   阶段 3  检查火绒等安全软件，安装 Docker Desktop
#   阶段 4  提示启动 Docker Desktop，给出验证命令
#
# 注意：本文件必须保存为 UTF-8 带 BOM，否则 Windows PowerShell 5.1 按 ANSI 解码，
# 中文字符串会把引号吃坏、脚本直接解析失败。

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$msi = Join-Path $root 'vendor\wsl_update_x64.msi'
$msiUrl = 'https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi'

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Head([string]$msg) {
    Write-Host ""
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 0. 提权检查
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Head "环境检查"
$reg = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
Say "  系统      : $($reg.ProductName) / $($reg.EditionID) / build $($reg.CurrentBuild).$($reg.UBR)"
Say "  当前用户  : $($identity.Name)"
Say "  管理员    : $(if ($isAdmin) { '是' } else { '否 —— 本脚本必须提权运行' })" $(if ($isAdmin) { 'Green' } else { 'Red' })

if (-not $isAdmin) {
    Say ""
    Say "请重开一个【以管理员身份运行】的 PowerShell，再执行同名命令：" 'Yellow'
    Say "  cd `"$root`"" 'Yellow'
    Say "  powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1" 'Yellow'
    exit 1
}

# ------------------------------------------------------- 1. 可选功能（本地 DISM）
function Get-FeatureState([string]$name) {
    try {
        (Get-WindowsOptionalFeature -Online -FeatureName $name -ErrorAction Stop).State
    } catch { "Unknown" }
}

Head "阶段 1 / 4  WSL 与虚拟机平台"
$wslState = Get-FeatureState 'Microsoft-Windows-Subsystem-Linux'
$vmpState = Get-FeatureState 'VirtualMachinePlatform'
Say "  Microsoft-Windows-Subsystem-Linux : $wslState"
Say "  VirtualMachinePlatform             : $vmpState"

$needEnable = @()
if ($wslState -ne 'Enabled') { $needEnable += 'Microsoft-Windows-Subsystem-Linux' }
if ($vmpState -ne 'Enabled') { $needEnable += 'VirtualMachinePlatform' }

$rebootNeeded = $false
foreach ($f in $needEnable) {
    Say "  启用 $f ..." 'Yellow'
    $out = & dism.exe /online /enable-feature /featurename:$f /all /norestart 2>&1 | Out-String
    if ($LASTEXITCODE -eq 3010) { $rebootNeeded = $true }
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) {
        Say "  [失败] dism 退出码 $LASTEXITCODE" 'Red'
        if ($out -match '拒绝|denied|0x8') { Say "  提示：若是火绒/Defender 拦截，先在安全软件里放行。" 'Yellow' }
        Say ($out.Trim() -split "`r?`n" | Select-Object -Last 6 | Out-String)
        exit 1
    }
    Say "  [OK] $f" 'Green'
}

# --------------------------------------------------------------- 2. 内核 MSI
Head "阶段 2 / 4  WSL2 内核"
$kernelInstalled = Test-Path 'C:\Windows\System32\lxss\tools\kernel'
Say "  内核文件是否存在 : $kernelInstalled"

if (-not $kernelInstalled) {
    if (-not (Test-Path $msi)) {
        Say "  本地没有 MSI，尝试下载 ..." 'Yellow'
        New-Item -ItemType Directory -Force (Split-Path $msi) | Out-Null
        try {
            Invoke-WebRequest $msiUrl -OutFile $msi -TimeoutSec 180 -UseBasicParsing
        } catch {
            Say "  下载失败：$($_.Exception.Message)" 'Red'
            Say "  请手动下载后放到：$msi" 'Yellow'
            exit 1
        }
    }
    $sig = Get-AuthenticodeSignature $msi
    Say "  MSI 签名 : $($sig.Status) / $($sig.SignerCertificate.Subject)" $(if ($sig.Status -eq 'Valid') { 'Green' } else { 'Red' })
    if ($sig.Status -ne 'Valid') { Say "  签名无效，拒绝安装。请重新下载。" 'Red'; exit 1 }

    Say "  安装内核 MSI ..." 'Yellow'
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
    Say "  msiexec 退出码 : $($p.ExitCode)"
    if ($p.ExitCode -eq 3010) { $rebootNeeded = $true }
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
        Say "  [失败] 内核没装上。常见原因：安全软件拦截服务安装。" 'Red'
        exit 1
    }
    Say "  [OK] 内核已安装" 'Green'
}

# 重启判定
$pending = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
) | Where-Object { Test-Path $_ }

if ($rebootNeeded -or $pending) {
    Head "需要重启"
    Say "  可选功能/内核已就位，但必须重启才生效（检测到重启标记）。" 'Yellow'
    Say ""
    Say "  1) 现在重启这台机器" 'Yellow'
    Say "  2) 重启后重开【管理员 PowerShell】，再跑一次本脚本（会自动进入阶段 3）" 'Yellow'
    Say ""
    Say "  重启前建议先在【火绒】里把 C:\Program Files\Docker 加入信任，否则" 'Yellow'
    Say "  Docker 的服务注册大概率会被 HIPS 拦掉（上次就是这么卡住的）。" 'Yellow'
    exit 2
}

# ------------------------------------------------------- 3. WSL2 自检 + 安全软件
Head "阶段 3 / 4  WSL2 与安全软件自检"
$wslStatus = (& wsl.exe --status 2>&1 | Out-String)
if ($wslStatus -match '未安装|not installed|适用于 Linux') {
    Say "  [警告] wsl --status 仍报未安装：可能仍需要重启，或功能未生效。" 'Yellow'
    Say ($wslStatus.Trim()) 'DarkGray'
} else {
    Say "  [OK] WSL 可用" 'Green'
    Say "  默认版本 : $((& wsl.exe --list --verbose 2>&1 | Out-String).Trim().Split("`n")[0])" 'DarkGray'
}

$av = Get-Process HipsDaemon, HipsTray, ZhuDongFangYu, 360tray -ErrorAction SilentlyContinue
if ($av) {
    Say ""
    Say "  [注意] 检测到安全软件：$((($av | ForEach-Object { $_.ProcessName }) | Select-Object -Unique) -join ', ')" 'Yellow'
    Say "  它上次把 Docker 的服务注册拦死了（安装器空转 21 分钟、零 CPU）。" 'Yellow'
    Say "  请在这台机器上：火绒 -> 安全设置 -> 信任区 / 或临时退出实时防护 10 分钟。" 'Yellow'
}

# ------------------------------------------------------ 4. Docker Desktop
Head "阶段 4 / 4  Docker Desktop"
$svc = Get-Service com.docker.service -ErrorAction SilentlyContinue
$dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'

if (-not $svc) {
    Say "  未检测到 com.docker.service，开始安装 Docker Desktop ..." 'Yellow'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id Docker.DockerDesktop -e --source winget --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            Say "  [失败] winget 退出码 $LASTEXITCODE" 'Red'
            Say "  可改用官方安装器手动安装：https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" 'Yellow'
        }
    } else {
        Say "  没有 winget，请手动下载安装：" 'Yellow'
        Say "  https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" 'Yellow'
    }
}

$svc = Get-Service com.docker.service -ErrorAction SilentlyContinue
if ($svc) {
    Say "  [OK] com.docker.service 已注册（$($svc.Status)）" 'Green'
    Say ""
    Say "  接下来这一步必须你手动做一次（GUI，脚本点不了）：" 'Yellow'
    Say "    1) 开始菜单启动 Docker Desktop" 'Yellow'
    Say "    2) 接受许可协议，等托盘鲸鱼图标变绿" 'Yellow'
    Say ""
    Say "  然后回来跑验证（一条命令，会用真的 pgvector/pgvector:pg17 镜像）：" 'Green'
    Say "    powershell -ExecutionPolicy Bypass -File scripts\verify-all.ps1 -Docker" 'Green'
} else {
    Say ""
    Say "  [未完成] 服务未注册。请检查安全软件是否拦截，然后重跑本脚本。" 'Red'
    exit 1
}

Write-Host ""
Write-Host ('-' * 72) -ForegroundColor DarkGray
Say "本阶段完成。" 'Green'
