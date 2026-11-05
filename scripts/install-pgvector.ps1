# 给本机 PostgreSQL 18 装上 pgvector（Windows 源码编译）
#
#   ## 必须以管理员身份运行 ##
#   powershell -ExecutionPolicy Bypass -File scripts/install-pgvector.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/install-pgvector.ps1 -Check   # 只体检，不改动
#
# 为什么必须提权：
#   1) 编译需要 MSVC（nmake/cl），本机现在没有任何 C 工具链，要装 VS Build Tools；
#   2) 装扩展要往 E:\software\postsql\lib 和 share\extension 写文件，当前用户不可写。
#
# pgvector 没有官方 Windows 二进制，源码编译是唯一可靠路径。步骤：
#   装 VS Build Tools（VC++ 工作负载） -> git clone -> nmake /F Makefile.win
#   -> nmake /F Makefile.win install -> 回到 psql 执行 CREATE EXTENSION vector
#
# 注意：本文件必须存为 UTF-8 带 BOM，否则 PowerShell 5.1 按 ANSI 解码会解析失败。

param(
    [switch]$Check
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PGCLIENTENCODING = 'UTF8'

function Say([string]$m, [string]$c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Head([string]$m) {
    Write-Host ""
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Host "  $m" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkGray
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

# ---------------------------------------------------------------- 1. 定位 PG
Head "1/5  定位 PostgreSQL 与 pg_config"
$candidates = @()
$svc = Get-Service 'postgresql*' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($svc) {
    $img = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$($svc.Name)" -ErrorAction SilentlyContinue).ImagePath
    if ($img -and $img -match '"?([A-Za-z]:\\[^"]*?)\\bin\\pg_ctl\.exe') { $candidates += $Matches[1] }
}
$candidates += 'E:\software\postsql', 'C:\Program Files\PostgreSQL\18', 'C:\Program Files\PostgreSQL\17'
$pgRoot = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path (Join-Path $c 'bin\pg_config.exe'))) { $pgRoot = $c; break }
}
if (-not $pgRoot) { Say "  找不到 pg_config.exe，请确认 PostgreSQL 装在哪。" 'Red'; exit 1 }
$pgConfig = Join-Path $pgRoot 'bin\pg_config.exe'
Say "  PGROOT    : $pgRoot" 'Green'
Say "  版本      : $((& $pgConfig --version 2>&1) -join ' ')"
Say "  libdir    : $((& $pgConfig --pkglibdir 2>&1) -join ' ')"
Say "  sharedir  : $((& $pgConfig --sharedir 2>&1) -join ' ')"

$extDir = Join-Path ((& $pgConfig --sharedir 2>&1) -join '') 'extension'
if (Test-Path (Join-Path $extDir 'vector.control')) {
    Say "  pgvector  : 已安装（vector.control 存在）" 'Green'
    Say "  下一步：psql -U postgres -d <你的库> -c `"CREATE EXTENSION IF NOT EXISTS vector;`"" 'Yellow'
    if ($Check) { exit 0 }
    exit 0
}
Say "  pgvector  : 未安装 —— 继续编译" 'Yellow'

# ------------------------------------------------------------ 2. 工具链检查
Head "2/5  编译工具链"
# vswhere 在本机会返回空（安装器没刷新它的 catalog），所以不能只靠它：
# 先问 vswhere，再直接扫安装目录，最后用"真编一个程序"做最终判定。
$vcvars = $null
$vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path $vswhere) {
    $vsPath = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>&1) -join ''
    if ($vsPath -and (Test-Path (Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'))) {
        $vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
    }
}
if (-not $vcvars) {
    $vcvars = Get-ChildItem 'C:\Program Files (x86)\Microsoft Visual Studio','C:\Program Files\Microsoft Visual Studio' `
            -Recurse -Filter 'vcvars64.bat' -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $vcvars -or -not (Test-Path $vcvars)) {
    Say "  MSVC      : 未找到（本机没有 VS / Build Tools）" 'Red'
    Say ""
    Say "  [x] 用 CLion 不行：CLion 自带的是 MinGW/GCC，而 Windows 上的" 'Yellow'
    Say "      PostgreSQL 扩展必须用 MSVC 编（要和服务器的 CRT/ABI 一致，" 'Yellow'
    Say "      Makefile.win 里就是 nmake + cl，还要链 postgres.lib）。" 'Yellow'
    Say ""
    Say "  装法（约 1.5-2 GB）：" 'Yellow'
    Say '  winget install --id Microsoft.VisualStudio.2022.BuildTools -e --source winget --accept-package-agreements --accept-source-agreements --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.22621"' 'White'
    if ($Check) { exit 3 }
    exit 3
}
Say "  MSVC      : $vcvars" 'Green'

# 真编一个 #include <stdio.h> + <windows.h> 的小程序 —— 只有这样才能证明
# 「编译器 + Windows SDK（UCRT 头 + import lib）」三件套齐全。
# 只看 vcvars64.bat 存在会漏掉"没装 Windows SDK"这种情况（本机就踩过）。
$probe = Join-Path $env:TEMP 'pgvector-toolchain-probe'
New-Item -ItemType Directory -Force $probe | Out-Null
@'
#include <stdio.h>
#include <windows.h>
#include <math.h>
int main(void) { printf("ok\n"); return 0; }
'@ | Set-Content (Join-Path $probe 'probe.c') -Encoding ascii
$probeOut = cmd /c "call `"$vcvars`" >nul 2>&1 && cd /d `"$probe`" && cl /nologo /W0 probe.c 2>&1" | Out-String
$toolchainOK = (Test-Path (Join-Path $probe 'probe.exe'))

if (-not $toolchainOK) {
    Say "  [x] 编译测试失败 —— 编译器在，但缺 Windows SDK（UCRT 头/库）" 'Red'
    ($probeOut -split "`r?`n" | Where-Object { $_ -match 'error|无法打开' } | Select-Object -First 3) | ForEach-Object { Say "      $($_.Trim())" 'DarkGray' }
    Say ""
    Say "  诊断：VC 工具链已装，但 Windows SDK 没装（C:\Program Files (x86)\Windows Kits\10 里没有 Include/Lib）。" 'Yellow'
    Say "  补装 SDK（管理员 PowerShell，约 1 GB）：" 'Yellow'
    Say '  & "C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe" modify --installPath "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools" --add Microsoft.VisualStudio.Component.Windows11SDK.22621 --quiet --norestart' 'White'
    Say ""
    Say "  （或改用 winget 重新声明组件：）" 'DarkGray'
    Say '  winget install --id Microsoft.VisualStudio.2022.BuildTools -e --source winget --accept-package-agreements --accept-source-agreements --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.22621"' 'DarkGray'
    if ($Check) { exit 4 }
    exit 4
}
Say "  编译测试  : 通过（stdio.h + windows.h + math.h 都可编译）" 'Green'

if ($Check) {
    Say ""
    Say "  体检完成：PGROOT 有、MSVC 有、Windows SDK 有，可以编译。" 'Green'
    exit 0
}

if (-not $isAdmin) {
    Say ""
    Say "  [需要提权] 装扩展要写入 $extDir，请用管理员 PowerShell 重跑。" 'Red'
    exit 1
}

# ------------------------------------------------------------- 3. 取源码
Head "3/5  获取 pgvector 源码"
# 优先用工作区里已经编好的产物（本项目已把源码放在 vendor\pgvector）：
# 这样提权那一跑只做文件拷贝，既快又不依赖 github（本机 git clone 会被断流）。
$prebuilt = Join-Path $PWD 'vendor\pgvector'
if (Test-Path (Join-Path $prebuilt 'vector.dll')) {
    Say "  用已编译产物 : $prebuilt" 'Green'
    Say "  （跳过 clone 与编译）" 'DarkGray'
    $src = $prebuilt
    $skipBuild = $true
} else {
    $skipBuild = $false
    $src = Join-Path $env:TEMP 'pgvector-src'
    if (Test-Path $src) { Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        Say "  git clone https://github.com/pgvector/pgvector ..." 'Yellow'
        & git clone --depth 1 --branch v0.8.7 https://github.com/pgvector/pgvector $src 2>&1 | Select-Object -Last 3
        if ($LASTEXITCODE -ne 0) {
            Say "  git 被断流，改用 codeload 的 zip ..." 'Yellow'
            $zip = Join-Path $env:TEMP 'pgvector.zip'
            Invoke-WebRequest 'https://codeload.github.com/pgvector/pgvector/zip/refs/tags/v0.8.7' -OutFile $zip -UseBasicParsing -TimeoutSec 180
            $stage = Join-Path $env:TEMP 'pgvector-unzip'
            if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
            Expand-Archive $zip -DestinationPath $stage -Force
            $src = (Get-ChildItem $stage -Directory | Select-Object -First 1).FullName
        }
    } else {
        Say "  没有 git，下载 zip ..." 'Yellow'
        $zip = Join-Path $env:TEMP 'pgvector.zip'
        Invoke-WebRequest 'https://codeload.github.com/pgvector/pgvector/zip/refs/tags/v0.8.7' -OutFile $zip -UseBasicParsing -TimeoutSec 180
        $stage = Join-Path $env:TEMP 'pgvector-unzip'
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
        Expand-Archive $zip -DestinationPath $stage -Force
        $src = (Get-ChildItem $stage -Directory | Select-Object -First 1).FullName
    }
}
if (-not (Test-Path (Join-Path $src 'Makefile.win'))) { Say "  源码不完整：$src" 'Red'; exit 1 }
Say "  源码      : $src" 'Green'

# ------------------------------------------------------------- 4. 编译安装
Head "4/5  编译并安装"
$log = Join-Path $PWD 'pgvector-build.log'

if ($skipBuild) {
    # 预编译产物已就位：**不走 nmake**，直接按 Makefile.win 的 install 目标逐文件拷贝。
    # 之前这里偷懒省掉 vcvars 却仍调 nmake，结果 nmake 不在 PATH 上 —— 纯拷贝最稳。
    Say "  直接安装预编译产物（不需要 nmake / 编译器）" 'Green'
    $hdrDir = Join-Path $pgRoot 'include\server\extension\vector'
    $plan = @(
        @{ src = 'vector.dll';            dst = (Join-Path $pgRoot 'lib') },
        @{ src = 'vector.control';        dst = (Join-Path $pgRoot 'share\extension') },
        @{ src = 'sql\vector--0.8.7.sql'; dst = (Join-Path $pgRoot 'share\extension') },
        @{ src = 'src\vector.h';          dst = $hdrDir },
        @{ src = 'src\halfvec.h';         dst = $hdrDir },
        @{ src = 'src\sparsevec.h';       dst = $hdrDir }
    )
    $fail = 0
    $report = @()
    foreach ($item in $plan) {
        $s = Join-Path $src $item.src
        if (-not (Test-Path $s)) { Say "  [跳过] 源文件不存在：$($item.src)" 'DarkGray'; continue }
        try {
            New-Item -ItemType Directory -Force $item.dst -ErrorAction Stop | Out-Null
            Copy-Item $s $item.dst -Force -ErrorAction Stop
            $report += "  [OK] $($item.src) -> $($item.dst)"
            Say "  [OK] $($item.src) -> $($item.dst)" 'Green'
        } catch {
            $fail++
            Say "  [失败] $($item.src): $($_.Exception.Message)" 'Red'
        }
    }
    $report | Set-Content $log -Encoding UTF8
    $OK = ($fail -eq 0) -and (Test-Path (Join-Path $extDir 'vector.control'))
} else {
    $cmd = @(
        "call `"$vcvars`""
        "cd /d `"$src`""
        "set `"PGROOT=$pgRoot`""
        "nmake /NOLOGO /F Makefile.win"
        "if errorlevel 1 exit /b 2"
        "nmake /NOLOGO /F Makefile.win install"
    ) -join ' && '
    Say "  执行 nmake（日志 $log）..." 'Yellow'
    $out = cmd /c $cmd 2>&1 | Out-String
    $out | Set-Content $log -Encoding UTF8
    $OK = ($out -notmatch 'fatal error|error C\d|NMAKE : fatal') -and `
          (Test-Path (Join-Path $extDir 'vector.control'))
    if (-not $OK) {
        ($out -split "`r?`n" | Select-Object -Last 20) | ForEach-Object { Say "    $_" 'DarkGray' }
    }
}

if (-not $OK) {
    Say "  [失败] 安装没完成。完整日志：$log" 'Red'
    if (-not $isAdmin) { Say "  提示：需要管理员权限才能写 $(Join-Path $pgRoot 'lib')" 'Yellow' }
    exit 1
}
Say "  [OK] vector.control 已就位" 'Green'

# ------------------------------------------------------------- 5. 收尾
Head "5/5  在数据库里启用扩展（需要 postgres 超级用户密码）"
Say "  编译安装完成。剩下这一步要连库，密码我不该也不想知道，请你执行：" 'Yellow'
Say ""
Say "  # 1) 建一个本项目专用角色和库（不用把超级用户密码告诉我）" 'White'
Say "  `"$pgRoot\bin\psql.exe`" -U postgres -c `"CREATE ROLE mem LOGIN PASSWORD 'mem';`"" 'White'
Say "  `"$pgRoot\bin\psql.exe`" -U postgres -c `"CREATE DATABASE mem OWNER mem;`"" 'White'
Say ""
Say "  # 2) 在库里建扩展（superuser 才能建）" 'White'
Say "  `"$pgRoot\bin\psql.exe`" -U postgres -d mem -c `"CREATE EXTENSION IF NOT EXISTS vector;`"" 'White'
Say "  `"$pgRoot\bin\psql.exe`" -U postgres -d mem -c `"CREATE EXTENSION IF NOT EXISTS pg_trgm;`"" 'White'
Say ""
Say "  # 3) 让我接手验证（一条命令，跑真 PG 18.6）" 'Green'
Say "  powershell -ExecutionPolicy Bypass -File scripts\verify-all.ps1 -Dsn `"postgresql://mem:mem@127.0.0.1:5432/mem`"" 'Green'
Write-Host ""
Say "完成。" 'Green'
