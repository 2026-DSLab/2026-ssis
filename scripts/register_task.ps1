<#
.SYNOPSIS
    주간 배치를 Windows 작업 스케줄러에 등록/해제한다.

.DESCRIPTION
    scripts\weekly.cmd 를 매주 정해진 요일·시각에 실행하도록 등록한다.
    weekly.cmd 는 감지 → 비교 → LLM 요약 → HWPX 보고서 → DB 적재까지
    한 번에 수행하고, 결과를 out\logs\weekly_<날짜>.log 에 남긴다.

    관리자 권한이 필요하지 않다. 대신 기본값은 "로그인한 사용자로
      실행"이라 해당 계정이 로그오프 상태면 작업이 미뤄진다. 서버에
      상시 무인 실행이 필요하면 -RunWhetherLoggedOn 을 쓰거나(계정
      비밀번호를 물어본다), 전용 서비스 계정으로 등록해야 한다.

    등록 전에 반드시 확인할 것: weekly.cmd 는 `python` 을 PATH 에서
      찾는다(.venv 가 있으면 그것을 우선). 작업 스케줄러는 대화형 셸의
      PATH 를 물려받지 않으므로, conda 환경에서만 python 이 잡히는
      상태라면 등록 후에도 실패한다. -Verify 로 미리 점검하라.

.PARAMETER TaskName
    작업 이름. 기본값 LawtrackWeekly.

.PARAMETER DayOfWeek
    실행 요일. 기본값 Monday.

.PARAMETER Time
    실행 시각(HH:mm). 기본값 06:00.

.PARAMETER RunWhetherLoggedOn
    로그오프 상태에서도 실행. 계정 비밀번호를 입력받아 저장한다.

.PARAMETER Verify
    등록하지 않고, 등록해도 정상 동작할 환경인지만 점검한다.

.PARAMETER Remove
    등록된 작업을 삭제한다.

.EXAMPLE
    # 먼저 점검
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Verify

.EXAMPLE
    # 매주 월요일 06:00 등록
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1

.EXAMPLE
    # 매주 금요일 18:30 등록
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -DayOfWeek Friday -Time 18:30

.EXAMPLE
    # 해제
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'LawtrackWeekly',
    [ValidateSet('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
    [string]$DayOfWeek = 'Monday',
    [ValidatePattern('^\d{1,2}:\d{2}$')]
    [string]$Time = '06:00',
    [switch]$RunWhetherLoggedOn,
    [switch]$Verify,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $PSScriptRoot 'weekly.cmd'

function Write-Ok    ($m) { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Write-Warn  ($m) { Write-Host "  [경고] $m" -ForegroundColor Yellow }
function Write-Fail  ($m) { Write-Host "  [실패] $m" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 해제
# ---------------------------------------------------------------------------
if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "등록된 작업이 없습니다: $TaskName"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "작업을 삭제했습니다: $TaskName" -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------------
# 사전 점검 — 등록하기 전에 "등록해도 돌아갈 환경인가"를 본다.
#
# 스케줄러 등록의 흔한 실패는 등록 자체가 아니라 등록 후 첫 실행에서
# 난다(python 을 못 찾음, .env 없음, 패키지 미설치). 그런데 그때는
# 아무도 보고 있지 않으므로 로그를 열어보기 전까지 모른다. 그래서
# 등록 시점에 먼저 확인한다.
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '환경 점검' -ForegroundColor Cyan
Write-Host ('-' * 70)

$problems = 0

if (Test-Path $cmd) {
    Write-Ok "배치 파일: $cmd"
} else {
    Write-Fail "배치 파일이 없습니다: $cmd"
    $problems++
}

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    $python = $venvPy
    Write-Ok "python: $python (프로젝트 .venv)"
} else {
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) {
        $python = $onPath.Source
        Write-Ok "python: $python (PATH)"
        Write-Warn 'PATH 의 python 을 씁니다. 작업 스케줄러는 시스템 PATH 만 보므로,'
        Write-Warn 'conda activate 로만 잡히는 python 이라면 무인 실행에서 실패합니다.'
    } else {
        Write-Fail 'python 을 찾을 수 없습니다 (PATH 에도 .venv 에도 없음).'
        $python = $null
        $problems++
    }
}

if ($python) {
    # 실제로 import 되는지 본다 — pyproject.toml 이 비어 있던 동안에는
    # pip install -e . 가 불가능해 이 import 가 실패했다.
    & $python -c "import lawtrack, summarizer" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'import lawtrack, summarizer 성공'
    } else {
        Write-Fail 'import 실패 — 프로젝트 루트에서 다음을 실행하세요:'
        Write-Fail '    pip install -e ".[openai]"'
        $problems++
    }
}

$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    Write-Ok ".env 있음: $envFile"
    $envText = Get-Content $envFile -Raw -Encoding UTF8
    foreach ($key in @('LAW_API_OC', 'MYSQL_PASSWORD')) {
        if ($envText -notmatch "(?m)^\s*$key\s*=\s*\S") {
            Write-Warn "$key 가 비어 있습니다 — 배치가 설정 오류로 종료됩니다."
            $problems++
        }
    }
    # 요약 단계는 프로바이더에 맞는 키가 필요하다.
    $provider = 'openai'
    if ($envText -match "(?m)^\s*SUMMARY_PROVIDER\s*=\s*(\S+)") { $provider = $Matches[1] }
    $keyName = if ($provider -eq 'anthropic') { 'ANTHROPIC_API_KEY' } else { 'OPENAI_API_KEY' }
    if ($envText -match "(?m)^\s*$keyName\s*=\s*\S") {
        Write-Ok "$keyName 설정됨 (SUMMARY_PROVIDER=$provider)"
    } else {
        Write-Warn "$keyName 가 비어 있습니다 — 감지까지만 되고 요약 단계에서 실패합니다."
        $problems++
    }
} else {
    Write-Fail ".env 가 없습니다: $envFile  (.env.example 을 복사해 채우세요)"
    $problems++
}

Write-Host ('-' * 70)
if ($problems -gt 0) {
    Write-Host "점검 결과: 문제 $problems 건" -ForegroundColor Yellow
} else {
    Write-Host '점검 결과: 이상 없음' -ForegroundColor Green
}

if ($Verify) {
    Write-Host ''
    Write-Host '(-Verify 모드라 등록하지 않고 종료합니다.)'
    # 삼항 연산자(?:)는 PowerShell 7+ 전용이라 쓰지 않는다 — 이 스크립트는
    # 기본 설치된 Windows PowerShell 5.1 에서도 돌아가야 한다.
    if ($problems -gt 0) { exit 1 } else { exit 0 }
}

if ($problems -gt 0) {
    Write-Host ''
    Write-Host '문제를 먼저 해결한 뒤 다시 등록하세요. 지금 등록하면 첫 실행이 실패합니다.' -ForegroundColor Yellow
    Write-Host '그래도 등록하려면 문제를 해결한 것으로 보고 다시 실행하면 됩니다.'
    exit 1
}

# ---------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '작업 등록' -ForegroundColor Cyan
Write-Host ('-' * 70)

# 작업 디렉터리를 프로젝트 루트로 고정한다. weekly.cmd 도 자체적으로
# cd 하지만, 스케줄러가 system32 에서 시작하면 상대 경로 로그가 엉뚱한
# 곳에 생기므로 양쪽 모두에서 못박아 둔다.
$action = New-ScheduledTaskAction -Execute $cmd -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $Time

# 배치가 몇 분 걸리고(워치리스트 102건 × API 호출) 네트워크가 필요하다.
# 노트북에서 등록하는 경우가 많아 배터리 상태로 건너뛰지 않게 해 둔다.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

if ($RunWhetherLoggedOn) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    Write-Host "로그오프 상태에서도 실행하도록 등록합니다 (계정: $user)."
    Write-Host '계정 비밀번호를 입력하세요 — 스케줄러가 저장합니다.'
    $cred = Get-Credential -UserName $user -Message '작업 스케줄러 실행 계정'
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description '법령/행정규칙 개정 주간 배치 (감지→비교→요약→보고서)' `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
        -RunLevel Limited -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description '법령/행정규칙 개정 주간 배치 (감지→비교→요약→보고서)' -Force | Out-Null
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Ok "등록 완료: $TaskName"
Write-Host "  실행 대상 : $cmd"
Write-Host "  주기      : 매주 $DayOfWeek $Time"
Write-Host "  다음 실행 : $($info.NextRunTime)"
Write-Host "  상태      : $($task.State)"
Write-Host ''
Write-Host '확인/운영 명령:'
Write-Host "  즉시 1회 실행 : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  실행 이력     : Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  로그          : out\logs\weekly_<yyyyMMdd>.log"
Write-Host "  해제          : powershell -File scripts\register_task.ps1 -Remove"
