@echo off
rem ===========================================================================
rem  mem.cmd -- one-line entry point for the memory store.
rem
rem    mem doctor
rem    mem recall "timezone" --limit 3
rem    mem add "conclusion" --kind episode --source tool
rem    mem fact set pref.answer_style concise
rem
rem  Why this wrapper exists: running python scripts\mem.py directly requires
rem  three things to be true at once --
rem    * cwd happens to be the project root (otherwise the script path breaks)
rem    * PYTHONPATH contains .pylibs (otherwise "import psycopg" fails)
rem    * the console code page can show Chinese (Chinese Windows defaults to
rem      GBK, which turns UTF-8 output into mojibake)
rem  This script handles all three, so everyday use needs no setup.
rem
rem  NOTE: this file must stay pure ASCII. cmd.exe parses .cmd in the *current*
rem  code page, so UTF-8 Chinese in a rem/comment line corrupts the next lines.
rem ===========================================================================
setlocal

set "MEM_ROOT=%~dp0"
if "%MEM_ROOT:~-1%"=="\" set "MEM_ROOT=%MEM_ROOT:~0,-1%"

set "PYTHONPATH=%MEM_ROOT%\.pylibs;%MEM_ROOT%"
set "PYTHONIOENCODING=utf-8"
if not defined MEM_PG_DSN set "MEM_PG_DSN=postgresql://mem:mem@127.0.0.1:5432/mem"
if not defined MEM_USER_ID set "MEM_USER_ID=owner"

rem ---------------------------------------------------------------------------
rem  PYTHONDONTWRITEBYTECODE=1 -- keeps this script's disk writes at zero.
rem
rem  History worth knowing: this flag was added while chasing "every memory write
rem  from another workspace asks for permission". It is NOT the fix for that --
rem  the real cause was broken NTFS ACLs on .pylibs (see scripts\fix-pylibs-acl.ps1):
rem  packages installed by pip INSIDE a sandboxed session inherited the restricted
rem  token's default DACL (SYSTEM / Administrators / OWNER RIGHTS only, no
rem  EVERYONE), so a session from a DIFFERENT workspace could not even read them
rem  -> sandbox denial -> escalation -> approval prompt. Fixed by granting
rem  EVERYONE (OI)(CI)(RX) recursively on .pylibs.
rem
rem  The flag is still worth keeping: the Windows sandbox is a WRITE_RESTRICTED
rem  token, so any write outside the session workspace is denied, and refreshing
rem  app\__pycache__ would count as one. With bytecode off there is nothing to deny.
rem
rem  Do not "fix" permission friction by raising the sandbox to danger-full-access;
rem  the ACL fix plus this flag are enough.
rem ---------------------------------------------------------------------------
set "PYTHONDONTWRITEBYTECODE=1"

rem 65001 = UTF-8 code page; without it Chinese output is mojibake in GBK consoles
chcp 65001 >nul 2>&1

python "%MEM_ROOT%\scripts\mem.py" %*
exit /b %ERRORLEVEL%
