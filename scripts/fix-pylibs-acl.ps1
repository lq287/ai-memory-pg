# 修复 .pylibs 的读权限（ACL），让受限沙箱里的会话能读记忆库的依赖库
#
# 症状：
#   在别的工作区（不是 D:\DSH proj\AI记忆）里存记忆，每次都弹"申请权限"，理由写着
#   "记忆库脚本需要读取工作区外的 .pylibs\onnxruntime\...，沙箱拒绝"。
#
# 真正的原因（不是沙箱策略，是文件 ACL 坏了）：
#   DSH 的 Windows 沙箱用 WRITE_RESTRICTED token，其 restricting SID 里含 EVERYONE。
#   而被沙箱内 pip 安装出来的文件继承的是**受限 token 的默认 DACL**，只有
#   SYSTEM / Administrators / OWNER RIGHTS，没有 EVERYONE / Users 可读 ACE。
#   于是：
#     · 在记忆库工作区里跑 → token 里带着当初那个工作区的 write SID → 读得到 → 不弹
#     · 在别的工作区里跑 → write SID 不同 → 读 .pylibs 下的文件被拒 → 申请提权 → 弹
#
#   复现方式（用 DSH 自带的 ACL runner，不必开新会话）：
#     node <dsh>\node_modules\@deepseek-ai\dsh-sandbox-windows-acl\lib\runner.js `
#       --workspace C:\some\other\ws --temp D:\dsh-acl-temp --mode workspace-write -- `
#       cmd /c type "D:\DSH proj\AI记忆\.pylibs\onnxruntime\__init__.py"
#     → Access is denied.（修复前）/ READ_OK（修复后）
#
# 什么时候需要重跑这个脚本：
#   只要**在沙箱内的会话里**给 .pylibs 装过包（pip install --target .pylibs ...），
#   新装的包就会再次带上坏 ACL。装完跑一次本脚本即可。
#
# ⚠️ 本文件必须存成 UTF-8 带 BOM（中文注释），否则 PowerShell 5.1 解析失败。

param(
    [string]$Pylibs = (Join-Path $PSScriptRoot '..\.pylibs'),
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$Pylibs = (Resolve-Path $Pylibs).Path
Write-Host "目标: $Pylibs"

function Get-BadFiles([string]$root) {
    $bad = New-Object System.Collections.ArrayList
    foreach ($f in (Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue)) {
        try {
            $acl = Get-Acl $f.FullName -ErrorAction Stop
            $ok = $false
            foreach ($a in $acl.Access) {
                if ($a.IdentityReference.Value -match 'Everyone|S-1-1-0|Authenticated Users|S-1-5-11|BUILTIN\\Users') {
                    $ok = $true; break
                }
            }
            if (-not $ok) { [void]$bad.Add($f.FullName) }
        } catch { }
    }
    return $bad
}

$bad = Get-BadFiles $Pylibs
Write-Host "缺少 Everyone/Users 可读 ACE 的文件: $($bad.Count)"
if ($Check) {
    if ($bad.Count -gt 0) {
        $bad | Select-Object -First 10 | ForEach-Object { "  $_" }
        exit 1
    }
    Write-Host "无需修复。"
    exit 0
}
if ($bad.Count -eq 0) {
    Write-Host "无需修复。"
    exit 0
}

Write-Host "修复中（给 EVERYONE 递归授予 读+执行，含继承）..."
$out = icacls $Pylibs /grant '*S-1-1-0:(OI)(CI)(RX)' /T /C 2>&1
($out | Select-String -Pattern 'Successfully processed|Failed processing') | ForEach-Object { "  $($_.Line.Trim())" }

$left = (Get-BadFiles $Pylibs).Count
Write-Host "复查: 仍缺可读 ACE 的文件 = $left"
if ($left -ne 0) { exit 1 }
Write-Host "[OK] 完成。现在从任何工作区的受限沙箱里都能读 .pylibs 了。"
exit 0
