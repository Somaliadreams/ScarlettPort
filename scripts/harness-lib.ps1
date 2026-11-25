# Shared log-parsing, telemetry aggregation, and report-generation functions
# for scripts/harness.ps1 (Windows PowerShell 5.1+ compatible).

function Strip-AnsiCodes {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    return [regex]::Replace($Text, '\x1B\[[0-9;]*[A-Za-z]', '')
}

function Read-HarnessSharedText {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $fs = $null
    $sr = $null
    try {
        $fs = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8, $true)
        return $sr.ReadToEnd()
    } catch {
        return ''
    } finally {
        if ($sr) { $sr.Dispose() }
        if ($fs) { $fs.Dispose() }
    }
}

function Get-HarnessNumberStats {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) {
        return [ordered]@{
            Count = 0
            Avg   = $null
            Min   = $null
            Max   = $null
            P95   = $null
        }
    }
    $sorted = @($Values | Sort-Object)
    $sum = 0.0
    foreach ($v in $sorted) { $sum += $v }
    $avg = $sum / $sorted.Count
    $p95Idx = [int][math]::Ceiling(0.95 * $sorted.Count) - 1
    if ($p95Idx -lt 0) { $p95Idx = 0 }
    if ($p95Idx -ge $sorted.Count) { $p95Idx = $sorted.Count - 1 }
    return [ordered]@{
        Count = $sorted.Count
        Avg   = [math]::Round($avg, 2)
        Min   = [math]::Round($sorted[0], 2)
        Max   = [math]::Round($sorted[$sorted.Count - 1], 2)
        P95   = [math]::Round($sorted[$p95Idx], 2)
    }
}

function Parse-VictoryLog {
    param(
        [string]$LogText,
        [int]$WorldDrawThreshold = 500
    )

    $clean = Strip-AnsiCodes -Text $LogText
    $lines = @()
    if ($clean.Length -gt 0) {
        $lines = $clean -split "`r?`n"
    }

    $rxFastBoot = 'fast boot: in world after (\d+) frames, ([0-9.]+) s \(([^)]+)\)'
    $rxRebar    = 'Resizable BAR \(D3D12 GPU upload heaps\):\s*(.+)$'
    $rxGpuHeap  = 'gpu_upload_heaps=(true|false)'
    $rxBudget   = 'card_budget_mb=(?:Some\()?(\d+)\)?'
    $rxFrame    = '([0-9.]+) fps, ([0-9.]+) ms/frame \(([0-9.]+) preparing: copies ([0-9.]+) ms ([0-9.]+) MB, textures ([0-9.]+) ms ([0-9.]+) MB; ([0-9.]+) on the GPU; ([0-9.]+) copying back ([0-9.]+) MB\), ([0-9.]+) draws/frame, dropped ([0-9.]+) without a host pipeline \((\d+) being made\) and ([0-9.]+) indirect'
    $rxCard     = "host copies in the card's memory: (\d+) copies (\d+) MB, their bytes from before (\d+) MB; per frame: ([0-9.]+) MB sent up in ([0-9.]+) ranges, ([0-9.]+) pieces compared on the card, ([0-9.]+) of them fetched \(([0-9.]+) MB\) with ([0-9.]+) more waits for the card \(([0-9.]+) ms\)"
    $rxBlack    = 'black frame (\d+):'
    $rxOom      = '(?:error 1455|ERROR_COMMITMENT_LIMIT|out of memory|failed to commit)'
    $rxPanic    = '(?:panicked at|fatal runtime error|STATUS_ACCESS_VIOLATION|0xc0000005|0x887a0005|DXGI_ERROR_DEVICE_REMOVED|DXGI_ERROR_DEVICE_HUNG|DXGI_ERROR_DEVICE_RESET)'

    $inWorld = $false
    $worldBootFrames = $null
    $worldBootSec = $null
    $worldBootReason = $null

    $rebarSupported = $null
    $rebarMessage = $null
    $cardBudgetMb = $null

    $blackFrames = 0
    $oomHits = 0
    $errorHits = New-Object System.Collections.Generic.List[string]

    $allSamples = New-Object System.Collections.Generic.List[object]
    $worldSamples = New-Object System.Collections.Generic.List[object]
    $worldCardSamples = New-Object System.Collections.Generic.List[object]

    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }

        if ($line -match $rxRebar) {
            $rebarMessage = $Matches[1].Trim()
            if ($line -match $rxGpuHeap) {
                $rebarSupported = ($Matches[1] -eq 'true')
            }
            if ($line -match $rxBudget) {
                $cardBudgetMb = [int]$Matches[1]
            }
        }

        if (-not $inWorld -and $line -match $rxFastBoot) {
            $worldBootFrames = [int]$Matches[1]
            $worldBootSec = [double]$Matches[2]
            $worldBootReason = $Matches[3]
            if ($worldBootReason -eq 'render targets') {
                $inWorld = $true
            }
        }

        if ($line -match $rxBlack) {
            $blackFrames++
        }

        if ($line -match $rxOom) {
            $oomHits++
            if ($errorHits.Count -lt 10) { $errorHits.Add($line.Trim()) }
        } elseif ($line -match $rxPanic) {
            if ($errorHits.Count -lt 10) { $errorHits.Add($line.Trim()) }
        }

        if ($line -match $rxCard) {
            $cardSample = [ordered]@{
                Copies       = [int]$Matches[1]
                CardMb       = [int]$Matches[2]
                BeforeMb     = [int]$Matches[3]
                SentUpMb     = [double]$Matches[4]
                Ranges       = [double]$Matches[5]
                Compared     = [double]$Matches[6]
                Fetched      = [double]$Matches[7]
                FetchedMb    = [double]$Matches[8]
                Waits        = [double]$Matches[9]
                WaitMs       = [double]$Matches[10]
            }
            if ($inWorld) {
                $worldCardSamples.Add($cardSample)
            }
        }

        if ($line -match $rxFrame) {
            $sample = [ordered]@{
                Fps        = [double]$Matches[1]
                FrameMs    = [double]$Matches[2]
                CpuPrepMs  = [double]$Matches[3]
                CopyMs     = [double]$Matches[4]
                CopyMb     = [double]$Matches[5]
                TextureMs  = [double]$Matches[6]
                TextureMb  = [double]$Matches[7]
                GpuMs      = [double]$Matches[8]
                CopyBackMs = [double]$Matches[9]
                CopyBackMb = [double]$Matches[10]
                Draws      = [double]$Matches[11]
                Dropped    = [double]$Matches[12]
                Making     = [int]$Matches[13]
                Indirect   = [double]$Matches[14]
            }
            $allSamples.Add($sample)
            if ($sample.Draws -ge $WorldDrawThreshold) {
                $inWorld = $true
            }
            if ($inWorld -and $sample.Draws -ge $WorldDrawThreshold) {
                $worldSamples.Add($sample)
            }
        }
    }

    # Drop the first world sample if we have >= 4 samples (often includes world-entry streaming spike)
    $steadySamples = $worldSamples
    if ($worldSamples.Count -ge 4) {
        $steadySamples = New-Object System.Collections.Generic.List[object]
        for ($i = 1; $i -lt $worldSamples.Count; $i++) {
            $steadySamples.Add($worldSamples[$i])
        }
    }

    $fpsStats      = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.Fps })
    $msStats       = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.FrameMs })
    $cpuStats      = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.CpuPrepMs })
    $copyStats     = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.CopyMs })
    $texStats      = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.TextureMs })
    $gpuStats      = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.GpuMs })
    $backStats     = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.CopyBackMs })
    $drawsStats    = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.Draws })
    $droppedStats  = Get-HarnessNumberStats -Values @($steadySamples | ForEach-Object { $_.Dropped })
    $cardMbStats   = Get-HarnessNumberStats -Values @($worldCardSamples | ForEach-Object { [double]$_.CardMb })
    $cardWaitStats = Get-HarnessNumberStats -Values @($worldCardSamples | ForEach-Object { $_.WaitMs })

    $bottleneck = 'unknown'
    if ($steadySamples.Count -gt 0) {
        $parts = @(
            [pscustomobject]@{ Name = 'GPU-bound';      Ms = [double]$gpuStats.Avg }
            [pscustomobject]@{ Name = 'Copy/UMA-bound'; Ms = [double]$copyStats.Avg + [double]$backStats.Avg }
            [pscustomobject]@{ Name = 'Texture-bound';  Ms = [double]$texStats.Avg }
            [pscustomobject]@{ Name = 'CPU-prep-bound'; Ms = [math]::Max(0.0, [double]$cpuStats.Avg - [double]$copyStats.Avg - [double]$texStats.Avg) }
        ) | Sort-Object -Property Ms -Descending
        $bottleneck = $parts[0].Name
    }

    return [ordered]@{
        ReachedWorld       = ($inWorld -and $worldSamples.Count -gt 0)
        WorldBootFrames    = $worldBootFrames
        WorldBootSec       = $worldBootSec
        WorldBootReason    = $worldBootReason
        RebarSupported     = $rebarSupported
        RebarMessage       = $rebarMessage
        CardBudgetMb       = $cardBudgetMb
        TotalSamples       = $allSamples.Count
        WorldSampleCount   = $worldSamples.Count
        SteadySampleCount  = $steadySamples.Count
        WorldFpsAvg        = $fpsStats.Avg
        WorldFpsMin        = $fpsStats.Min
        WorldFpsMax        = $fpsStats.Max
        WorldMsAvg         = $msStats.Avg
        WorldMsP95         = $msStats.P95
        CpuPrepMsAvg       = $cpuStats.Avg
        CopyMsAvg          = $copyStats.Avg
        TextureMsAvg       = $texStats.Avg
        GpuMsAvg           = $gpuStats.Avg
        CopyBackMsAvg      = $backStats.Avg
        DrawsAvg           = $drawsStats.Avg
        DroppedAvg         = $droppedStats.Avg
        CardMbMax          = $cardMbStats.Max
        CardWaitMsAvg      = $cardWaitStats.Avg
        Bottleneck         = $bottleneck
        BlackFrames        = $blackFrames
        OomHits            = $oomHits
        ErrorLines         = @($errorHits)
    }
}

function Get-HarnessVerdict {
    param(
        [System.Collections.IDictionary]$Parsed,
        [string]$TerminationReason,
        [Nullable[int]]$ExitCode,
        [int]$TdrCount,
        [int]$CrashDeltaLines
    )

    if ($Parsed.OomHits -gt 0) { return 'OOM_COMMIT' }
    if ($TdrCount -gt 0) { return 'TDR' }
    if ($ExitCode -ne $null -and ($ExitCode -eq -2005270523 -or $ExitCode -eq 2289696773)) { return 'DEVICE_REMOVED' }
    if ($TerminationReason -eq 'stall-boot') { return 'HUNG_BOOT' }
    if ($TerminationReason -eq 'stall-world') { return 'HUNG_WORLD' }
    if ($TerminationReason -eq 'boot-timeout') { return 'BOOT_TIMEOUT' }
    if ($TerminationReason -eq 'exited-early' -or $CrashDeltaLines -gt 0) { return 'CRASH' }
    if (-not $Parsed.ReachedWorld) { return 'NO_WORLD' }
    return 'PASS'
}

function Format-HarnessMetric {
    param($Value, [string]$Suffix = '')
    if ($null -eq $Value -or ($Value -is [string] -and $Value -eq '')) { return '-' }
    return "$Value$Suffix"
}

function Write-HarnessReports {
    param(
        [string]$SessionDir,
        [object]$HostInfo,
        [object[]]$Results
    )

    $csvPath = Join-Path $SessionDir 'summary.csv'
    $mdPath  = Join-Path $SessionDir 'SUMMARY.md'

    $csvRows = foreach ($r in $Results) {
        [pscustomobject]@{
            Profile           = $r.Profile
            Console           = $r.Console
            Verdict           = $r.Verdict
            Termination       = $r.TerminationReason
            BootSec           = $r.WorldBootSec
            WorldSamples      = $r.SteadySampleCount
            WorldFpsAvg       = $r.WorldFpsAvg
            WorldFpsMin       = $r.WorldFpsMin
            WorldFpsMax       = $r.WorldFpsMax
            WorldMsAvg        = $r.WorldMsAvg
            WorldMsP95        = $r.WorldMsP95
            GpuMsAvg          = $r.GpuMsAvg
            CpuPrepMsAvg      = $r.CpuPrepMsAvg
            CopyMsAvg         = $r.CopyMsAvg
            TextureMsAvg      = $r.TextureMsAvg
            CopyBackMsAvg     = $r.CopyBackMsAvg
            DrawsAvg          = $r.DrawsAvg
            DroppedAvg        = $r.DroppedAvg
            Bottleneck        = $r.Bottleneck
            PeakRamGb         = $r.PeakWorkingSetGb
            PeakCommitGb      = $r.PeakCommitGb
            CardMbMax         = $r.CardMbMax
            RebarSupported    = $r.RebarSupported
            BlackFrames       = $r.BlackFrames
            TdrCount          = $r.TdrCount
            CrashLogLines     = $r.CrashDeltaLines
            CapturedPngs      = $r.CapturedPngCount
            ExitCode          = $r.ExitCode
            ElapsedSec        = $r.ElapsedSec
        }
    }
    $csvRows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8

    $passing = @($Results | Where-Object { $_.Verdict -eq 'PASS' -and $null -ne $_.WorldFpsAvg } | Sort-Object -Property WorldFpsAvg -Descending)
    $winner = if ($passing.Count -gt 0) { $passing[0] } else { $null }

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("# ScarlettPort Unattended Test Harness Report")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    [void]$sb.AppendLine("")

    if ($HostInfo) {
        [void]$sb.AppendLine("## Host Hardware")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("- **GPU:** $($HostInfo.GpuName) (Driver: $($HostInfo.GpuDriver))")
        [void]$sb.AppendLine("- **CPU:** $($HostInfo.CpuName)")
        [void]$sb.AppendLine("- **RAM / Pagefile:** $($HostInfo.RamGb) GB RAM, $($HostInfo.PagefileGb) GB pagefile")
        [void]$sb.AppendLine("- **OS:** $($HostInfo.OsCaption) (Build $($HostInfo.OsBuild))")
        [void]$sb.AppendLine("")
    }

    if ($winner) {
        [void]$sb.AppendLine("## Recommended Best Profile")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("**``$($winner.Profile)``** ($($winner.Console)): **$($winner.WorldFpsAvg) fps** avg ($($winner.WorldMsAvg) ms/frame, P95 $($winner.WorldMsP95) ms), primary bottleneck: **$($winner.Bottleneck)**, peak RAM **$($winner.PeakWorkingSetGb) GB**, peak commit **$($winner.PeakCommitGb) GB**, TDRs: $($winner.TdrCount), black frames: $($winner.BlackFrames).")
        [void]$sb.AppendLine("")
    } else {
        [void]$sb.AppendLine("## Summary")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("No profile completed with a clean ``PASS`` verdict yet. Check individual logs below for crashes, TDRs, or boot timeouts.")
        [void]$sb.AppendLine("")
    }

    [void]$sb.AppendLine("## Results Matrix")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("| Profile | Console | Verdict | Boot (s) | FPS avg (min-max) | Frame ms (P95) | GPU ms | Copy+Back ms | Tex ms | Peak RAM / Commit | ReBAR | TDR / Black | Bottleneck | Artifacts |")
    [void]$sb.AppendLine("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|--|")

    foreach ($r in $Results) {
        $fpsCol = if ($null -ne $r.WorldFpsAvg) { "$($r.WorldFpsAvg) ($($r.WorldFpsMin)-$($r.WorldFpsMax))" } else { '-' }
        $msCol  = if ($null -ne $r.WorldMsAvg) { "$($r.WorldMsAvg) ($($r.WorldMsP95))" } else { '-' }
        $copyBackTotal = if ($null -ne $r.CopyMsAvg -and $null -ne $r.CopyBackMsAvg) { [math]::Round([double]$r.CopyMsAvg + [double]$r.CopyBackMsAvg, 1) } else { '-' }
        $memCol = "$(Format-HarnessMetric $r.PeakWorkingSetGb 'G') / $(Format-HarnessMetric $r.PeakCommitGb 'G')"
        $rebarCol = if ($null -eq $r.RebarSupported) { '?' } elseif ($r.RebarSupported) { 'yes' } else { 'no' }
        $artCol = "[log]($($r.Profile)/run.log)"
        if ($r.CapturedPngCount -gt 0) {
            $artCol += ", $($r.CapturedPngCount) png"
        }

        [void]$sb.AppendLine("| ``$($r.Profile)`` | $($r.Console) | **$($r.Verdict)** | $(Format-HarnessMetric $r.WorldBootSec) | $fpsCol | $msCol | $(Format-HarnessMetric $r.GpuMsAvg) | $copyBackTotal | $(Format-HarnessMetric $r.TextureMsAvg) | $memCol | $rebarCol | $($r.TdrCount) / $($r.BlackFrames) | $($r.Bottleneck) | $artCol |")
    }

    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("## Per-Profile Notes & Env Overrides")
    [void]$sb.AppendLine("")
    foreach ($r in $Results) {
        [void]$sb.AppendLine("### ``$($r.Profile)`` - $($r.Verdict)")
        if ($r.Description) {
            [void]$sb.AppendLine("- $($r.Description)")
        }
        if ($r.OverridesText) {
            [void]$sb.AppendLine("- **Overrides:** ``$($r.OverridesText)``")
        }
        if ($r.RebarMessage) {
            [void]$sb.AppendLine("- **ReBAR probe:** $($r.RebarMessage)")
        }
        if ($r.ErrorLines -and $r.ErrorLines.Count -gt 0) {
            [void]$sb.AppendLine("- **Notable errors:**")
            foreach ($err in $r.ErrorLines) {
                [void]$sb.AppendLine("  - ``$err``")
            }
        }
        [void]$sb.AppendLine("")
    }

    [System.IO.File]::WriteAllText($mdPath, $sb.ToString(), [System.Text.Encoding]::UTF8)
    return [ordered]@{
        MarkdownPath = $mdPath
        CsvPath      = $csvPath
    }
}

function Invoke-HarnessSelfTest {
    $sampleLog = @"
2026-11-25T13:10:01Z INFO victory_gpu::rebar: gpu_upload_heaps=false options16_known=true card_budget_mb=Some(15234) level=1 most_mb=3072 Resizable BAR (D3D12 GPU upload heaps): NOT available: VICTORY_REBAR is ignored, copies are made as before
2026-11-25T13:10:30Z INFO victory_gpu::host: 30.0 fps, 33.3 ms/frame (2.1 preparing: copies 0.4 ms 1.2 MB, textures 0.2 ms 0.5 MB; 1.8 on the GPU; 0.1 copying back 0.0 MB), 42 draws/frame, dropped 0 without a host pipeline (0 being made) and 0 indirect
2026-11-25T13:11:15Z WARN victory_gpu::queue: fast boot: in world after 1240 frames, 74.2 s (render targets)
2026-11-25T13:11:25Z INFO victory_gpu::host: host copies in the card's memory: 18 copies 1620 MB, their bytes from before 810 MB; per frame: 14.5 MB sent up in 62 ranges, 320 pieces compared on the card, 12 of them fetched (0.8 MB) with 1.0 more waits for the card (3.4 ms)
2026-11-25T13:11:25Z INFO victory_gpu::host: 3.1 fps, 322.5 ms/frame (60.0 preparing: copies 22.0 ms 45.0 MB, textures 18.0 ms 12.0 MB; 240.0 on the GPU; 14.0 copying back 2.1 MB), 4820 draws/frame, dropped 12 without a host pipeline (3 being made) and 110 indirect
2026-11-25T13:11:35Z INFO victory_gpu::host: 5.0 fps, 200.0 ms/frame (34.0 preparing: copies 14.0 ms 28.0 MB, textures 8.0 ms 4.0 MB; 150.0 on the GPU; 8.0 copying back 1.2 MB), 5100 draws/frame, dropped 0 without a host pipeline (0 being made) and 120 indirect
2026-11-25T13:11:45Z INFO victory_gpu::host: 5.4 fps, 185.2 ms/frame (30.0 preparing: copies 12.0 ms 24.0 MB, textures 6.0 ms 3.0 MB; 140.0 on the GPU; 6.0 copying back 0.9 MB), 5120 draws/frame, dropped 0 without a host pipeline (0 being made) and 120 indirect
2026-11-25T13:11:55Z INFO victory_gpu::host: 5.2 fps, 192.3 ms/frame (32.0 preparing: copies 13.0 ms 26.0 MB, textures 7.0 ms 3.5 MB; 146.0 on the GPU; 7.0 copying back 1.0 MB), 5090 draws/frame, dropped 0 without a host pipeline (0 being made) and 120 indirect
"@

    $parsed = Parse-VictoryLog -LogText $sampleLog
    if (-not $parsed.ReachedWorld) { throw 'SelfTest failed: ReachedWorld should be true' }
    if ($parsed.WorldBootFrames -ne 1240) { throw "SelfTest failed: expected 1240 boot frames, got $($parsed.WorldBootFrames)" }
    if ($parsed.SteadySampleCount -ne 3) { throw "SelfTest failed: expected 3 steady world samples, got $($parsed.SteadySampleCount)" }
    if ($parsed.RebarSupported -ne $false) { throw 'SelfTest failed: RebarSupported should be false' }
    if ($parsed.CardBudgetMb -ne 15234) { throw "SelfTest failed: expected CardBudgetMb 15234, got $($parsed.CardBudgetMb)" }
    if ($parsed.Bottleneck -ne 'GPU-bound') { throw "SelfTest failed: expected GPU-bound, got $($parsed.Bottleneck)" }
    if ([math]::Abs($parsed.WorldFpsAvg - 5.2) -gt 0.05) { throw "SelfTest failed: expected avg FPS ~5.2, got $($parsed.WorldFpsAvg)" }

    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("scarlett-harness-selftest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    try {
        $verdict = Get-HarnessVerdict -Parsed $parsed -TerminationReason 'completed' -ExitCode 0 -TdrCount 0 -CrashDeltaLines 0
        if ($verdict -ne 'PASS') { throw "SelfTest failed: expected PASS, got $verdict" }
        $row = [ordered]@{}
        foreach ($k in $parsed.Keys) { $row[$k] = $parsed[$k] }
        $row['Profile']           = 'selftest-profile'
        $row['Description']       = 'Synthetic log self-test'
        $row['Console']           = 'series-s'
        $row['Verdict']           = $verdict
        $row['TerminationReason'] = 'completed'
        $row['PeakWorkingSetGb']  = 11.4
        $row['PeakCommitGb']      = 18.2
        $row['TdrCount']          = 0
        $row['CrashDeltaLines']   = 0
        $row['CapturedPngCount']  = 4
        $row['ExitCode']          = 0
        $row['ElapsedSec']        = 135
        $row['OverridesText']     = 'VICTORY_CARD_BUFFERS_MB=4096'

        $out = Write-HarnessReports -SessionDir $tmpDir -HostInfo ([ordered]@{
            GpuName = 'RX 6800 XT'; GpuDriver = 'test'; CpuName = 'Ryzen 5 7600X';
            RamGb = 32; PagefileGb = 32; OsCaption = 'Windows 10'; OsBuild = '19045'
        }) -Results @([pscustomobject]$row)

        if (-not (Test-Path -LiteralPath $out.MarkdownPath)) { throw 'SelfTest failed: SUMMARY.md not written' }
        if (-not (Test-Path -LiteralPath $out.CsvPath)) { throw 'SelfTest failed: summary.csv not written' }
    } finally {
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $true
}
