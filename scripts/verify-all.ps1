param(
    [switch]$Detailed,
    [switch]$Docker,
    [string]$Dsn
)

# 全量验证：一条命令跑完所有能跑的东西。
#
#   powershell -ExecutionPolicy Bypass -File scripts/verify-all.ps1            # PGlite（默认，无需 Docker）
#   powershell -ExecutionPolicy Bypass -File scripts/verify-all.ps1 -Docker    # 真 pgvector/pgvector:pg17 镜像
#   powershell -ExecutionPolicy Bypass -File scripts/verify-all.ps1 -Dsn "postgresql://mem:mem@127.0.0.1:5432/mem"  # 本机/远端已有的 PG
#   powershell -ExecutionPolicy Bypass -File scripts/verify-all.ps1 -Detailed  # 逐项细节
#
# 覆盖：语法编译 -> 零依赖单测 -> 八份 SQL + 19 组行为断言 -> Python<->PG 全 API 面
#       -> 端到端 demo -> worker CLI。PGlite 模式下最后一定会停掉它自己起的服务。
#
# 注意：本文件必须保存为 UTF-8 **带 BOM**，否则 Windows PowerShell 5.1 会按 ANSI
# 解码，中文字符串会把引号吃坏、脚本直接解析失败。
#
# 另一条铁律：**永远不要** `Get-Process node | Stop-Process` —— 你的宿主/编辑器
# 很可能本身就是 node 进程，一把梭会把界面连同会话一起杀掉。只按 Start-Process
# 返回的 PID 定点关闭。

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:npm_config_cache = Join-Path $PWD '.npm-cache'
$env:PYTHONPATH = "$PWD\.pylibs;$PWD"
$env:MEM_EMBED_MODEL = 'hash-768'

# 默认 55433 而不是 55432：后者常被 docker compose 或上次没退干净的实例占着
$port = if ($env:MEM_VERIFY_PORT) { $env:MEM_VERIFY_PORT } else { '55433' }
$env:MEM_PGLITE_PORT = $port
$env:MEM_PG_DSN = "postgresql://postgres@127.0.0.1:$port/postgres"
# PGlite 的 wire 服务端不隔离会话级预处理语句名：第二个连接的自动 PREPARE 会撞名
# （prepared statement "_pg3_0" already exists）。只在 PGlite 模式下关掉自动 PREPARE；
# 连真 PostgreSQL（-Dsn/-Docker）时保持 psycopg 默认，别为兼容性牺牲生产性能。
$env:MEM_PG_NO_PREPARE = '1'
if ($Dsn -or $Docker) { $env:MEM_PG_NO_PREPARE = '' }   # 真 PG 上保留自动 PREPARE

$failures = New-Object System.Collections.ArrayList
$global:LASTEXITCODE = 0

function Step([string]$name, [scriptblock]$body) {
    Write-Host ""
    Write-Host ("-" * 74) -ForegroundColor DarkGray
    Write-Host "> $name" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    try {
        & $body
        if ($global:LASTEXITCODE -ne 0) { throw "exit code $global:LASTEXITCODE" }
        Write-Host "  [OK] $name" -ForegroundColor Green
    } catch {
        Write-Host "  [FAIL] $name -- $_" -ForegroundColor Red
        [void]$failures.Add($name)
    }
}

Write-Host "验证工作目录：$PWD" -ForegroundColor DarkGray

# --- 1. 语法 ---------------------------------------------------------------
Step 'python -m compileall' {
    python -m compileall -q app tests scripts
}

# --- 2. 零依赖单测 ---------------------------------------------------------
Step 'tests/test_decide.py (28)' {
    python tests\test_decide.py 2>&1 | Select-Object -Last 2
}
Step 'tests/test_pipeline.py (33)' {
    python tests\test_pipeline.py 2>&1 | Select-Object -Last 2
}
Step 'tests/test_distill.py (25：接地校验/后端选择)' {
    python tests\test_distill.py 2>&1 | Select-Object -Last 2
}
Step 'tests/test_digest.py (21：抽词/子串抑制/tf-idf/桶标签)' {
    python tests\test_digest.py 2>&1 | Select-Object -Last 2
}

# --- 3. SQL 层 -------------------------------------------------------------
Step 'npm run check (8 SQL + 19 assertion groups + 7 evidence groups)' {
    node scripts/sqlcheck.mjs 2>&1 |
        Select-String -Pattern '> sql/|宿主|结果：|扩展：|自定义函数|✗' | ForEach-Object { $_.Line }
}

# --- 4. 起服务，验证 Python <-> PG ----------------------------------------
# -Docker：用真的 pgvector/pgvector:pg17 容器（生产路线）
# 默认：用 PGlite（PG 18 + pgvector 0.8.7 编译成 WASM，不需要 Docker）
$server = $null
$log = Join-Path $PWD '.tmp-server.log'
$err = Join-Path $PWD '.tmp-server.err'
try {
    Write-Host ""
    Write-Host ("-" * 74) -ForegroundColor DarkGray

    if ($Dsn) {
        # 用已经存在的 PostgreSQL（本机装好的 PG18、或远端库）
        Write-Host "> 使用已有 PostgreSQL：$Dsn" -ForegroundColor Cyan
        $psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
        if (-not $psql) {
            $guess = @('E:\software\postsql\bin\psql.exe') +
                     (Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' -ErrorAction SilentlyContinue |
                        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
            $psql = $guess | Where-Object { Test-Path $_ } | Select-Object -First 1
        }
        if (-not $psql) { throw "找不到 psql.exe，请把 PostgreSQL\bin 加进 PATH" }
        Write-Host "  psql: $psql" -ForegroundColor DarkGray
        $env:MEM_PG_DSN = $Dsn
        $env:PGCLIENTENCODING = 'UTF8'

        Step '装载 01..08 + 跑 99_selftest.sql（psql，真 PG）' {
            Write-Host "  $((& $psql $Dsn -tAc 'SELECT version()' 2>&1) -join ' ')"
            foreach ($f in '01_schema.sql', '02_retrieval.sql', '03_write.sql', '04_workmem.sql', '05_distill.sql', '06_graph.sql', '07_purge.sql', '08_digest.sql', '99_selftest.sql') {
                $out = & $psql $Dsn -v ON_ERROR_STOP=1 -f (Join-Path $PWD "sql\$f") 2>&1 | Out-String
                if ($global:LASTEXITCODE -ne 0) {
                    Write-Host ($out -split "`r?`n" | Select-Object -Last 20)
                    throw "$f 执行失败"
                }
                Write-Host "  $f -> OK（PASS 计数 $(([regex]::Matches($out, 'PASS')).Count)）"
            }
        }
    }
    elseif ($Docker) {
        Write-Host "> 启动 docker compose (pgvector/pgvector:pg17)" -ForegroundColor Cyan
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker 未安装" }
        docker compose up -d --wait 2>&1 | ForEach-Object { "  $_" }
        if ($global:LASTEXITCODE -ne 0) { throw "docker compose up 失败 (exit $global:LASTEXITCODE)" }
        $env:MEM_PG_DSN = 'postgresql://mem:mem@127.0.0.1:55432/mem'
        Write-Host "  [OK] 容器就绪" -ForegroundColor Green

        Step '在真镜像上装载 01..08 + 跑 99_selftest.sql' {
            foreach ($f in '01_schema.sql', '02_retrieval.sql', '03_write.sql', '04_workmem.sql', '05_distill.sql', '06_graph.sql', '07_purge.sql', '08_digest.sql', '99_selftest.sql') {
                $out = docker compose exec -T db psql -U mem -d mem -v ON_ERROR_STOP=1 -f "/sql/$f" 2>&1 | Out-String
                if ($global:LASTEXITCODE -ne 0) {
                    Write-Host $out
                    throw "$f 执行失败"
                }
                $pass = ([regex]::Matches($out, 'PASS')).Count
                Write-Host "  $f -> OK（PASS 计数 $pass）"
            }
            $ver = docker compose exec -T db psql -U mem -d mem -tAc "SELECT version()" 2>&1
            Write-Host "  $($ver -join ' ')"
        }
    }
    else {
        Write-Host "> 启动 PGlite wire 服务 (127.0.0.1:$port)" -ForegroundColor Cyan
        $server = Start-Process -FilePath 'node' -ArgumentList 'scripts/pgserver.mjs' `
            -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 500
            if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
                $ready = $true
                break
            }
        }
        if (-not $ready) { throw "port $port not ready, see $err" }
        Write-Host "  [OK] 服务就绪 (pid=$($server.Id))" -ForegroundColor Green
    }

    $pyArgs = @('scripts\verify_all.py')
    if ($Detailed) { $pyArgs += '-v' }

    Step 'scripts/verify_all.py (Python<->PG 全 API 面)' {
        & python @pyArgs 2>&1 |
            Select-String -Pattern '\[FAIL\]|结果：|全部通过|^DSN|^user' | ForEach-Object { $_.Line }
    }

    Step 'scripts/verify_workmem.py (任务状态 + 工作记忆)' {
        python scripts\verify_workmem.py 2>&1 |
            Select-String -Pattern 'FAIL|结果：' | ForEach-Object { $_.Line }
    }

    Step 'scripts/verify_distill.py (提炼：候选/血缘/幂等/plan+apply)' {
        python scripts\verify_distill.py 2>&1 |
            Select-String -Pattern 'FAIL|结果：' | ForEach-Object { $_.Line }
    }

    Step 'scripts/verify_graph.py (图扩展 + 整用户清除)' {
        python scripts\verify_graph.py 2>&1 |
            Select-String -Pattern '\[FAIL\]|全部通过|失败 ' | ForEach-Object { $_.Line }
    }

    Step 'scripts/verify_digest.py (日/周/月/年关键词提炼)' {
        python scripts\verify_digest.py 2>&1 |
            Select-String -Pattern '\[FAIL\]|全部通过|失败 ' | ForEach-Object { $_.Line }
    }

    Step 'python -m app.demo (四态 + TTL + 冲突裁决 + 预算)' {
        python -m app.demo 2>&1 |
            Select-String -Pattern '处理了|裁决:|timezone |^   \[|结果为|Traceback|Error' |
            ForEach-Object { $_.Line }
    }

    Step 'python -m app.worker --once (黑盒 CLI)' {
        python -m app.worker --once --log-level WARNING
    }
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "已停止 PGlite 服务 (pid=$($server.Id))" -ForegroundColor DarkGray
    }
    Remove-Item $log, $err -ErrorAction SilentlyContinue
}

# --- 5. 汇总 --------------------------------------------------------------
Write-Host ""
Write-Host ("-" * 74) -ForegroundColor DarkGray
if ($failures.Count -gt 0) {
    Write-Host "失败 $($failures.Count) 项：" -ForegroundColor Red
    foreach ($f in $failures) { Write-Host "  - $f" -ForegroundColor Red }
    exit 1
}
Write-Host "全部验证通过。" -ForegroundColor Green
exit 0
