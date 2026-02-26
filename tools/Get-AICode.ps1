function Get-AICode {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prompt,
        [switch]$PassThru
    )

    Write-Host "`n╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  Prompt: $Prompt" -ForegroundColor White
    Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Cyan

    $body = "{`"prompt`":`"$Prompt`"}"
    $r = Invoke-RestMethod -Uri "http://localhost:8000/generate" -Method POST -ContentType "application/json" -Body $body
    Write-Host "Request ID: $($r.request_id)" -ForegroundColor Gray
    Write-Host "Generating code... (30-60 seconds)`n" -ForegroundColor Yellow

    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 10
        try {
            $s = Invoke-RestMethod -Uri "http://localhost:8000/status/$($r.request_id)"
            Write-Host "  [$(($i + 1) * 10)s] $($s.status)..." -ForegroundColor DarkGray

            if ($s.status -eq "completed") {
                Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
                Write-Host "SUCCESS! Generated in $(($i + 1) * 10) seconds" -ForegroundColor Green
                Write-Host "Language: $($s.language) | Iterations: $($s.iterations)" -ForegroundColor Yellow
                Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
                Write-Host ""
                Write-Host $s.code -ForegroundColor White
                Write-Host ""

                if ($PassThru) {
                    return $s.code
                }
                return
            }
            elseif ($s.status -eq "failed") {
                Write-Host "`n✗ FAILED: $($s.errors)" -ForegroundColor Red
                return
            }
        }
        catch { }
    }

    Write-Host "`n⚠ Timeout - check status later with:" -ForegroundColor Yellow
    Write-Host "  Invoke-RestMethod http://localhost:8000/status/$($r.request_id)" -ForegroundColor Gray
}
