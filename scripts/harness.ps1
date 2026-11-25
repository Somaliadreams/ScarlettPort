<#
.SYNOPSIS
    Unattended test, benchmark, and stability harness for ScarlettPort (VIctory runtime).

.DESCRIPTION
    Runs a matrix of environment-variable profiles back-to-back with zero babysitting:
    - Auto-builds victory.exe + Agility SDK 1.614.1 (Windows 10) and runs one-time shader
      warm-up + graphics pipeline precompile unless skipped.
    - Uses VICTORY_FAST_BOOT=1 + VICTORY_SKIP_CUTSCENES=1 to reach the open world automatically.
    - Monitors RAM/commit, stalls/hangs, crashes, Windows Display TDRs (Event ID 4101),
      ReBAR support, and steady-state world FPS/frame-time breakdowns.
    - Optionally injects movement inputs (-WalkTest) once in world to test streaming.
    - Saves per-profile result.json + run.log + world screenshots, and regenerates
      SUMMARY.md + summary.csv after EVERY profile (safe against crashes/reboots, resumable).

.EXAMPLE
    .\scripts\harness.ps1 -SelfTest
.EXAMPLE
    .\scripts\harness.ps1 -Game "D:\Games\MyDump"
.EXAMPLE
    .\scripts\harness.ps1 -Game "D:\Games\MyDump" -Only "01-baseline-series-s,02-vram-4gb-series-s" -WalkTest
.EXAMPLE
    .\scripts\harness.ps1 -Game "D:\Games\MyDump" -Resume ".\logs\harness\20261125-180000"
.EXAMPLE
    .\scripts\harness.ps1 -AnalyzeOnly ".\logs\harness\20261125-180000"
#>
param(
    [string]$Game = '',
    [string]$Matrix = '',
    [string]$Only = '',
    [string]$Resume = '',
    [string]$AnalyzeOnly = '',
    [int]$WorldMeasureSec = 60,
    [int]$CutsceneGraceSec = 30,
    [int]$BootTimeoutSec = 420,
    [int]$StallTimeoutSec = 120,
    [int]$MaxRunSec = 600,
    [int]$CooldownSec = 10,
    [switch]$WalkTest,
    [switch]$Rebuild,
    [switch]$SkipSetup,
    [switch]$SkipWarm,
    [switch]$SkipPrecompile,
    [switch]$NoCapture,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'harness-lib.ps1')

if ($SelfTest) {
    Write-Host '==> Running harness self-test...' -ForegroundColor Cyan
    [void](Invoke-HarnessSelfTest)
    Write-Host 'PASS: harness parser, verdict scorer, and report generator verified.' -ForegroundColor Green
    exit 0
}

function Get-HostHardwareInfo {
    $gpuName = 'Unknown'
    $gpuDriver = 'Unknown'
    $cpuName = 'Unknown'
    $ramGb = 0
    $pagefileGb = 0
    $osCaption = 'Windows'
    $osBuild = ''

    try {
        $gpus = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction SilentlyContinue)
        $picked = $gpus | Where-Object { $_.Name -match 'Radeon|NVIDIA|GeForce|RTX|RX |Arc ' } | Select-Object -First 1
        if (-not $picked) { $picked = $gpus | Select-Object -First 1 }
        if ($picked) {
            $gpuName = $picked.Name
            $gpuDriver = $picked.DriverVersion
        }
    } catch {}

    try {
        $cpu = Get-CimInstance -ClassName Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cpu) { $cpuName = $cpu.Name.Trim() }
    } catch {}

    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cs) { $ramGb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1) }
    } catch {}

    try {
        $pfs = @(Get-CimInstance -ClassName Win32_PageFileUsage -ErrorAction SilentlyContinue)
        $pfMb = 0
        foreach ($pf in $pfs) { $pfMb += [double]$pf.AllocatedBaseSize }
        $pagefileGb = [math]::Round($pfMb / 1024.0, 1)
    } catch {}

    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($os) {
            $osCaption = $os.Caption.Trim()
            $osBuild = $os.BuildNumber
        }
    } catch {}

    return [ordered]@{
        GpuName    = $gpuName
        GpuDriver  = $gpuDriver
        CpuName    = $cpuName
        RamGb      = $ramGb
        PagefileGb = $pagefileGb
        OsCaption  = $osCaption
        OsBuild    = $osBuild
    }
}

function Get-TdrEventsSince {
    param([datetime]$Since)
    try {
        $events = @(Get-WinEvent -FilterHashtable @{
            LogName      = 'System'
            ProviderName = 'Display'
            Id           = 4101
            StartTime    = $Since
        } -ErrorAction SilentlyContinue)
        return $events.Count
    } catch {
        return 0
    }
}

function Get-CrashLogLineCount {
    $dataDir = if ($env:VICTORY_DATA_DIR) { $env:VICTORY_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'victory\data' }
    $crashLog = Join-Path $dataDir 'crashes.log'
    if (-not (Test-Path -LiteralPath $crashLog)) { return 0 }
    $txt = Read-HarnessSharedText -Path $crashLog
    if ([string]::IsNullOrEmpty($txt)) { return 0 }
    return ($txt -split "`r?`n").Count
}

function Stop-ProcessTreeSafe {
    param([int]$ProcessId)
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($p -and -not $p.HasExited) {
            & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
            Start-Sleep -Milliseconds 500
            $p2 = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
            if ($p2 -and -not $p2.HasExited) {
                Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}
}

function Get-MergedRunLog {
    param(
        [string]$StdoutPath,
        [string]$StderrPath
    )
    $a = Read-HarnessSharedText -Path $StdoutPath
    $b = Read-HarnessSharedText -Path $StderrPath
    if ([string]::IsNullOrEmpty($a)) { return $b }
    if ([string]::IsNullOrEmpty($b)) { return $a }
    return ($a + "`r`n" + $b)
}

function Invoke-AnalyzeSession {
    param([string]$SessionDir)
    if (-not (Test-Path -LiteralPath $SessionDir)) {
        throw "Session folder not found: $SessionDir"
    }
    $hostInfoPath = Join-Path $SessionDir 'host.json'
    $hostInfo = if (Test-Path -LiteralPath $hostInfoPath) {
        Get-Content -LiteralPath $hostInfoPath -Raw | ConvertFrom-Json
    } else {
        $null
    }

    $results = New-Object System.Collections.Generic.List[object]
    $dirs = @(Get-ChildItem -LiteralPath $SessionDir -Directory | Sort-Object Name)
    foreach ($d in $dirs) {
        $resPath = Join-Path $d.FullName 'result.json'
        $logPath = Join-Path $d.FullName 'run.log'
        if (-not (Test-Path -LiteralPath $logPath) -and -not (Test-Path -LiteralPath $resPath)) { continue }

        $prev = if (Test-Path -LiteralPath $resPath) {
            Get-Content -LiteralPath $resPath -Raw | ConvertFrom-Json
        } else { $null }

        $logText = if (Test-Path -LiteralPath $logPath) { Read-HarnessSharedText -Path $logPath } else { '' }
        $parsed = Parse-VictoryLog -LogText $logText

        $term = if ($prev -and $prev.TerminationReason) { $prev.TerminationReason } else { 'analyzed' }
        $exitCode = if ($prev) { $prev.ExitCode } else { $null }
        $tdr = if ($prev) { [int]$prev.TdrCount } else { 0 }
        $crashDelta = if ($prev) { [int]$prev.CrashDeltaLines } else { 0 }
        $verdict = Get-HarnessVerdict -Parsed $parsed -TerminationReason $term -ExitCode $exitCode -TdrCount $tdr -CrashDeltaLines $crashDelta

        $framesDir = Join-Path $d.FullName 'frames'
        $pngCount = if (Test-Path -LiteralPath $framesDir) { @(Get-ChildItem -LiteralPath $framesDir -Filter '*.png' -ErrorAction SilentlyContinue).Count } else { 0 }

        $row = [ordered]@{}
        foreach ($k in $parsed.Keys) { $row[$k] = $parsed[$k] }
        $row['Profile']           = $d.Name
        $row['Description']       = if ($prev) { $prev.Description } else { '' }
        $row['Console']           = if ($prev -and $prev.Console) { $prev.Console } else { 'unknown' }
        $row['Verdict']           = $verdict
        $row['TerminationReason'] = $term
        $row['PeakWorkingSetGb']  = if ($prev) { $prev.PeakWorkingSetGb } else { $null }
        $row['PeakCommitGb']      = if ($prev) { $prev.PeakCommitGb } else { $null }
        $row['TdrCount']          = $tdr
        $row['CrashDeltaLines']   = $crashDelta
        $row['CapturedPngCount']  = $pngCount
        $row['ExitCode']          = $exitCode
        $row['ElapsedSec']        = if ($prev) { $prev.ElapsedSec } else { $null }
        $row['OverridesText']     = if ($prev) { $prev.OverridesText } else { '' }

        $obj = [pscustomobject]$row
        $obj | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resPath -Encoding UTF8
        $results.Add($obj)
    }

    $out = Write-HarnessReports -SessionDir $SessionDir -HostInfo $hostInfo -Results @($results)
    Write-Host "Rebuilt reports:" -ForegroundColor Green
    Write-Host "  $($out.MarkdownPath)"
    Write-Host "  $($out.CsvPath)"
}

if ($AnalyzeOnly) {
    Invoke-AnalyzeSession -SessionDir $AnalyzeOnly
    exit 0
}

if (-not $Game) {
    throw 'Pass -Game "D:\path\to\your\dump" (or -SelfTest / -AnalyzeOnly <sessionDir>).'
}
$exe = Join-Path $Game 'game_scarlett_master.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    throw "No game_scarlett_master.exe found in '$Game'."
}

if (-not $Matrix) {
    $Matrix = Join-Path $PSScriptRoot 'harness-matrix.json'
}
if (-not (Test-Path -LiteralPath $Matrix)) {
    throw "Matrix config not found: $Matrix"
}

$matrixObj = Get-Content -LiteralPath $Matrix -Raw | ConvertFrom-Json
$allProfiles = @($matrixObj.profiles)
if ($Only) {
    $wanted = @($Only -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $allProfiles = @($allProfiles | Where-Object { $wanted -contains $_.name })
    if ($allProfiles.Count -eq 0) {
        throw "No profiles matched -Only '$Only'."
    }
}

$hostInfo = Get-HostHardwareInfo
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' ScarlettPort Unattended Test Harness' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host "GPU:      $($hostInfo.GpuName) (Driver $($hostInfo.GpuDriver))"
Write-Host "CPU:      $($hostInfo.CpuName)"
Write-Host "Memory:   $($hostInfo.RamGb) GB RAM + $($hostInfo.PagefileGb) GB pagefile"
Write-Host "OS:       $($hostInfo.OsCaption) (Build $($hostInfo.OsBuild))"
Write-Host "Profiles: $($allProfiles.Count) from $Matrix"

if ($hostInfo.RamGb -gt 0 -and ($hostInfo.RamGb + $hostInfo.PagefileGb) -lt 48) {
    Write-Host "WARNING: Total commit (RAM + pagefile = $([math]::Round($hostInfo.RamGb + $hostInfo.PagefileGb, 1)) GB) is under 48 GB." -ForegroundColor Yellow
    Write-Host "         Consider raising your Windows pagefile to 32-48 GB on SSD to avoid error 1455." -ForegroundColor Yellow
}

$vic = Join-Path $root 'runtime\victory\target\release\victory.exe'
$agilityDll = Join-Path $root 'runtime\victory\target\release\D3D12\D3D12Core.dll'
$logsRoot = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null

# 1. One-time build + Agility SDK + shader warm + pipeline precompile
if (-not $SkipSetup) {
    if ($Rebuild -or -not (Test-Path -LiteralPath $vic) -or -not (Test-Path -LiteralPath $agilityDll)) {
        Write-Host '==> Building victory.exe + fetching Agility SDK 1.614.1...' -ForegroundColor Cyan
        & (Join-Path $PSScriptRoot 'setup-victory.ps1') -SkipPrecompile
    }

    $warmStamp = Join-Path $logsRoot 'warm.done'
    if (-not $SkipWarm -and -not (Test-Path -LiteralPath $warmStamp)) {
        Write-Host '==> Warming shader cache once (victory warm)...' -ForegroundColor Cyan
        $jobs = [math]::Max(1, [Environment]::ProcessorCount - 1)
        $env:VICTORY_GAME_DIR = $Game
        $ErrorActionPreference = 'Continue'
        & $vic warm $Game --jobs $jobs
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($code -ne 0) { throw "victory warm failed (exit $code)." }
        Set-Content -LiteralPath $warmStamp -Value (Get-Date -Format s)
    }

    $preStamp = Join-Path $logsRoot 'precompile-graphics.done'
    if (-not $SkipPrecompile -and -not (Test-Path -LiteralPath $preStamp)) {
        Write-Host '==> Precompiling graphics pipelines once (victory precompile-graphics)...' -ForegroundColor Cyan
        $env:VICTORY_GAME_DIR = $Game
        $ErrorActionPreference = 'Continue'
        & $vic precompile-graphics
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($code -ne 0) { throw "victory precompile-graphics failed (exit $code)." }
        Set-Content -LiteralPath $preStamp -Value (Get-Date -Format s)
    }
}

if (-not (Test-Path -LiteralPath $vic)) {
    throw "victory.exe not found at $vic. Remove -SkipSetup or run .\scripts\setup-victory.ps1 first."
}

# 2. Session directory (new or resumed)
$sessionDir = if ($Resume) {
    $Resume
} else {
    Join-Path $logsRoot ("harness\" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
New-Item -ItemType Directory -Force -Path $sessionDir | Out-Null
$hostInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $sessionDir 'host.json') -Encoding UTF8
Copy-Item -LiteralPath $Matrix -Destination (Join-Path $sessionDir 'matrix.json') -Force

Write-Host "Session:  $sessionDir" -ForegroundColor Green
Write-Host ''

# Load any already-completed results when resuming
$completedByName = @{}
if ($Resume) {
    foreach ($d in @(Get-ChildItem -LiteralPath $sessionDir -Directory -ErrorAction SilentlyContinue)) {
        $rp = Join-Path $d.FullName 'result.json'
        if (Test-Path -LiteralPath $rp) {
            $completedByName[$d.Name] = (Get-Content -LiteralPath $rp -Raw | ConvertFrom-Json)
        }
    }
}

$resultsList = New-Object System.Collections.Generic.List[object]
foreach ($profile in $allProfiles) {
    $name = [string]$profile.name
    if ($completedByName.ContainsKey($name)) {
        Write-Host "==> Skipping already-completed profile: $name" -ForegroundColor DarkGray
        $resultsList.Add($completedByName[$name])
        continue
    }

    $consoleProfile = if ($profile.console) { [string]$profile.console } elseif ($matrixObj.defaults.console) { [string]$matrixObj.defaults.console } else { 'series-s' }
    $runDir = Join-Path $sessionDir $name
    $framesDir = Join-Path $runDir 'frames'
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    if (-not $NoCapture) {
        New-Item -ItemType Directory -Force -Path $framesDir | Out-Null
    }

    $stdoutPath = Join-Path $runDir 'stdout.log'
    $stderrPath = Join-Path $runDir 'stderr.log'
    $mergedLogPath = Join-Path $runDir 'run.log'
    $pressFile = Join-Path $runDir 'press.txt'
    Set-Content -LiteralPath $pressFile -Value '' -NoNewline

    # Merge default env + profile overrides
    $mergedEnv = [ordered]@{}
    if ($matrixObj.defaults -and $matrixObj.defaults.env) {
        foreach ($p in $matrixObj.defaults.env.PSObject.Properties) {
            $mergedEnv[$p.Name] = [string]$p.Value
        }
    }
    $overridePairs = New-Object System.Collections.Generic.List[string]
    if ($profile.env) {
        foreach ($p in $profile.env.PSObject.Properties) {
            $mergedEnv[$p.Name] = [string]$p.Value
            $overridePairs.Add("$($p.Name)=$($p.Value)")
        }
    }

    # Harness-managed automation env vars
    $mergedEnv['VICTORY_GAME_DIR']         = $Game
    $mergedEnv['VICTORY_FAST_BOOT']        = '1'
    $mergedEnv['VICTORY_FAST_BOOT_MAX_S']  = [string]$BootTimeoutSec
    $mergedEnv['VICTORY_SKIP_CUTSCENES']   = '1'
    $mergedEnv['VICTORY_NO_WATCHDOG']      = '1'
    $mergedEnv['VICTORY_PRESS_FILE']       = $pressFile
    $mergedEnv['NO_COLOR']                 = '1'
    if (-not $NoCapture) {
        $mergedEnv['VICTORY_CAPTURE']       = $framesDir
        $mergedEnv['VICTORY_CAPTURE_WORLD'] = '1'
    }

    $mergedEnv | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $runDir 'env.json') -Encoding UTF8

    Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
    Write-Host "Running [$name] (--console $consoleProfile)" -ForegroundColor Cyan
    if ($profile.description) { Write-Host "  $($profile.description)" }
    if ($overridePairs.Count -gt 0) { Write-Host "  Overrides: $($overridePairs -join ' ')" }

    # Snapshot parent VICTORY_* / SCARLETT_* / NO_COLOR env vars for clean isolation
    $savedEnv = @{}
    foreach ($item in Get-ChildItem Env:) {
        if ($item.Name -like 'VICTORY_*' -or $item.Name -like 'SCARLETT_*' -or $item.Name -eq 'NO_COLOR') {
            $savedEnv[$item.Name] = $item.Value
            Remove-Item "Env:$($item.Name)" -ErrorAction SilentlyContinue
        }
    }

    $startTime = Get-Date
    $crashLinesBefore = Get-CrashLogLineCount
    $terminationReason = 'completed'
    $exitCode = $null
    $peakWsBytes = [int64]0
    $peakCommitBytes = [int64]0
    $proc = $null

    try {
        foreach ($kv in $mergedEnv.GetEnumerator()) {
            Set-Item "Env:$($kv.Key)" $kv.Value
        }

        $argList = @('run', $exe, '--execute', '--console', $consoleProfile)
        $proc = Start-Process -FilePath $vic `
            -ArgumentList $argList `
            -WorkingDirectory $root `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru

        $worldDetectedAt = $null
        $walkSent = $false
        $lastProgressAt = Get-Date
        $lastLogBytes = 0
        $lastStatusPrintAt = Get-Date

        while ($true) {
            Start-Sleep -Seconds 2
            $now = Get-Date
            $elapsedSec = ($now - $startTime).TotalSeconds

            # Sample RAM / commit
            $liveProc = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
            if ($liveProc) {
                if ($liveProc.WorkingSet64 -gt $peakWsBytes) { $peakWsBytes = $liveProc.WorkingSet64 }
                if ($liveProc.PrivateMemorySize64 -gt $peakCommitBytes) { $peakCommitBytes = $liveProc.PrivateMemorySize64 }
            }

            # Check log growth & world detection
            $curBytes = 0
            if (Test-Path -LiteralPath $stdoutPath) { $curBytes += (Get-Item -LiteralPath $stdoutPath).Length }
            if (Test-Path -LiteralPath $stderrPath) { $curBytes += (Get-Item -LiteralPath $stderrPath).Length }
            if ($curBytes -gt $lastLogBytes) {
                $lastLogBytes = $curBytes
                $lastProgressAt = $now
            }

            if ($null -eq $worldDetectedAt) {
                $curLog = Strip-AnsiCodes -Text (Get-MergedRunLog -StdoutPath $stdoutPath -StderrPath $stderrPath)
                if ($curLog -match 'fast boot: in world after \d+ frames, [0-9.]+ s \(render targets\)' -or
                    $curLog -match '[0-9.]+ fps, [0-9.]+ ms/frame \([^)]+\), ([5-9]\d{2}|[1-9]\d{3,})(?:\.\d+)? draws/frame') {
                    $worldDetectedAt = $now
                    Write-Host ("  [{0:0}s] World detected! Waiting {1}s cutscene grace + {2}s measurement..." -f $elapsedSec, $CutsceneGraceSec, $WorldMeasureSec) -ForegroundColor Green
                }
            } else {
                $inWorldSec = ($now - $worldDetectedAt).TotalSeconds
                if ($WalkTest -and -not $walkSent -and $inWorldSec -ge ($CutsceneGraceSec + [math]::Floor($WorldMeasureSec / 2))) {
                    Write-Host ("  [{0:0}s] Injecting walk/camera test tokens..." -f $elapsedSec) -ForegroundColor DarkCyan
                    Set-Content -LiteralPath $pressFile -Value 'LS_UP LS_UP RS_RIGHT LS_UP' -NoNewline
                    $walkSent = $true
                }
                if ($inWorldSec -ge ($CutsceneGraceSec + $WorldMeasureSec)) {
                    $terminationReason = 'completed'
                    Write-Host ("  [{0:0}s] Measurement window complete; stopping process." -f $elapsedSec) -ForegroundColor Green
                    Stop-ProcessTreeSafe -ProcessId $proc.Id
                    break
                }
            }

            if ($proc.HasExited) {
                $terminationReason = 'exited-early'
                $exitCode = $proc.ExitCode
                Write-Host ("  [{0:0}s] Process exited early (exit code {1})." -f $elapsedSec, $exitCode) -ForegroundColor Red
                break
            }

            $stallSec = ($now - $lastProgressAt).TotalSeconds
            $allowedStall = if ($null -eq $worldDetectedAt) { [math]::Max($StallTimeoutSec, 240) } else { $StallTimeoutSec }
            if ($stallSec -ge $allowedStall) {
                $terminationReason = if ($null -eq $worldDetectedAt) { 'stall-boot' } else { 'stall-world' }
                Write-Host ("  [{0:0}s] No log progress for {1:0}s ($terminationReason); terminating." -f $elapsedSec, $stallSec) -ForegroundColor Red
                Stop-ProcessTreeSafe -ProcessId $proc.Id
                break
            }

            if ($null -eq $worldDetectedAt -and $elapsedSec -ge $BootTimeoutSec) {
                $terminationReason = 'boot-timeout'
                Write-Host ("  [{0:0}s] Did not reach world within {1}s; terminating." -f $elapsedSec, $BootTimeoutSec) -ForegroundColor Red
                Stop-ProcessTreeSafe -ProcessId $proc.Id
                break
            }

            if ($elapsedSec -ge $MaxRunSec) {
                $terminationReason = 'max-run-timeout'
                Write-Host ("  [{0:0}s] Reached hard cap ({1}s); terminating." -f $elapsedSec, $MaxRunSec) -ForegroundColor Yellow
                Stop-ProcessTreeSafe -ProcessId $proc.Id
                break
            }

            if (($now - $lastStatusPrintAt).TotalSeconds -ge 20) {
                $lastStatusPrintAt = $now
                $stage = if ($null -eq $worldDetectedAt) { 'booting' } else { "in-world $([int]($now - $worldDetectedAt).TotalSeconds)s" }
                $wsGb = [math]::Round($peakWsBytes / 1GB, 1)
                $cmGb = [math]::Round($peakCommitBytes / 1GB, 1)
                Write-Host ("  [{0:0}s] {1} | peak RAM {2} GB / commit {3} GB" -f $elapsedSec, $stage, $wsGb, $cmGb) -ForegroundColor DarkGray
            }
        }

        if ($proc -and $null -eq $exitCode) {
            try {
                $proc.WaitForExit(3000) | Out-Null
                if ($proc.HasExited) { $exitCode = $proc.ExitCode }
            } catch {}
        }
    } finally {
        if ($proc -and -not $proc.HasExited) {
            Stop-ProcessTreeSafe -ProcessId $proc.Id
        }
        foreach ($item in Get-ChildItem Env:) {
            if ($item.Name -like 'VICTORY_*' -or $item.Name -like 'SCARLETT_*' -or $item.Name -eq 'NO_COLOR') {
                Remove-Item "Env:$($item.Name)" -ErrorAction SilentlyContinue
            }
        }
        foreach ($kv in $savedEnv.GetEnumerator()) {
            Set-Item "Env:$($kv.Key)" $kv.Value
        }
    }

    # Merge logs + analyze immediately
    $mergedText = Get-MergedRunLog -StdoutPath $stdoutPath -StderrPath $stderrPath
    [System.IO.File]::WriteAllText($mergedLogPath, $mergedText, [System.Text.Encoding]::UTF8)

    $tdrCount = Get-TdrEventsSince -Since $startTime
    $crashDelta = [math]::Max(0, (Get-CrashLogLineCount) - $crashLinesBefore)
    $parsed = Parse-VictoryLog -LogText $mergedText
    $verdict = Get-HarnessVerdict -Parsed $parsed -TerminationReason $terminationReason -ExitCode $exitCode -TdrCount $tdrCount -CrashDeltaLines $crashDelta
    $pngCount = if (-not $NoCapture -and (Test-Path -LiteralPath $framesDir)) { @(Get-ChildItem -LiteralPath $framesDir -Filter '*.png' -ErrorAction SilentlyContinue).Count } else { 0 }
    $elapsedTotal = [math]::Round(((Get-Date) - $startTime).TotalSeconds, 1)

    $row = [ordered]@{}
    foreach ($k in $parsed.Keys) { $row[$k] = $parsed[$k] }
    $row['Profile']           = $name
    $row['Description']       = if ($profile.description) { [string]$profile.description } else { '' }
    $row['Console']           = $consoleProfile
    $row['Verdict']           = $verdict
    $row['TerminationReason'] = $terminationReason
    $row['PeakWorkingSetGb']  = [math]::Round($peakWsBytes / 1GB, 2)
    $row['PeakCommitGb']      = [math]::Round($peakCommitBytes / 1GB, 2)
    $row['TdrCount']          = $tdrCount
    $row['CrashDeltaLines']   = $crashDelta
    $row['CapturedPngCount']  = $pngCount
    $row['ExitCode']          = $exitCode
    $row['ElapsedSec']        = $elapsedTotal
    $row['OverridesText']     = ($overridePairs -join ' ')

    $resultObj = [pscustomobject]$row
    $resultObj | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $runDir 'result.json') -Encoding UTF8
    $resultsList.Add($resultObj)

    # Regenerate SUMMARY.md + summary.csv after EVERY profile
    $reportPaths = Write-HarnessReports -SessionDir $sessionDir -HostInfo $hostInfo -Results @($resultsList)

    $verdictColor = if ($verdict -eq 'PASS') { 'Green' } else { 'Red' }
    Write-Host ("  => {0} | FPS: {1} avg | Frame: {2} ms | Bottleneck: {3} | Peak RAM/Commit: {4}/{5} GB | TDR: {6}" -f `
        $verdict, (Format-HarnessMetric $parsed.WorldFpsAvg), (Format-HarnessMetric $parsed.WorldMsAvg), `
        $parsed.Bottleneck, $row['PeakWorkingSetGb'], $row['PeakCommitGb'], $tdrCount) -ForegroundColor $verdictColor

    if ($CooldownSec -gt 0 -and $profile -ne $allProfiles[$allProfiles.Count - 1]) {
        Write-Host "  Cooling down ${CooldownSec}s before next profile..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $CooldownSec
    }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host ' Harness finished!' -ForegroundColor Green
Write-Host " Report: $(Join-Path $sessionDir 'SUMMARY.md')" -ForegroundColor Green
Write-Host " CSV:    $(Join-Path $sessionDir 'summary.csv')" -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Green
