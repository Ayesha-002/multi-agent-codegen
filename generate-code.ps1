# ════════════════════════════════════════════════════════════════
#  AI Code Generator - Submit Prompt & Wait for Result
# ════════════════════════════════════════════════════════════════

param(
    [Parameter(Mandatory=$true)]
    [string]$Prompt,
    
    [string]$Language = "",
    [int]$MaxWait = 180  # 3 minutes max
)

Write-Host "`n╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║         AI Multi-Agent Code Generator                     ║" -ForegroundColor Cyan
Write-Host "╚═══════════════════════════════════════════════════════════╝`n" -ForegroundColor Cyan

# Step 1: Submit prompt
Write-Host "[1/3] Submitting prompt..." -ForegroundColor Yellow
Write-Host "      Prompt: $Prompt" -ForegroundColor Gray

$body = @{
    prompt = $Prompt
}
if ($Language) {
    $body.language = $Language
}

try {
    $response = Invoke-RestMethod -Uri "http://localhost:8000/generate" `
                                  -Method POST `
                                  -ContentType "application/json" `
                                  -Body ($body | ConvertTo-Json)
    
    $requestId = $response.request_id
    Write-Host "      ✓ Request ID: $requestId`n" -ForegroundColor Green
}
catch {
    Write-Host "`n✗ ERROR: Could not submit request" -ForegroundColor Red
    Write-Host "  Make sure containers are running: docker-compose ps" -ForegroundColor Yellow
    exit 1
}

# Step 2: Wait for analysis
Write-Host "[2/3] Analyzing prompt..." -ForegroundColor Yellow
Start-Sleep -Seconds 3

# Step 3: Poll for completion
Write-Host "[3/3] Generating code (this takes 60-120 seconds)...`n" -ForegroundColor Yellow

$elapsed = 0
$lastStatus = ""

while ($elapsed -lt $MaxWait) {
    Start-Sleep -Seconds 10
    $elapsed += 10
    
    try {
        $status = Invoke-RestMethod -Uri "http://localhost:8000/status/$requestId"
        
        # Show progress
        $time = Get-Date -Format "HH:mm:ss"
        $stage = $status.status
        $iter = $status.iterations
        
        if ($stage -ne $lastStatus) {
            Write-Host "      [$time] Stage: $stage | Iteration: $iter" -ForegroundColor Cyan
            $lastStatus = $stage
        }
        
        # Check if done
        if ($status.status -eq "completed") {
            Write-Host "`n╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Green
            Write-Host "║                    ✓ SUCCESS!                             ║" -ForegroundColor Green
            Write-Host "╚═══════════════════════════════════════════════════════════╝`n" -ForegroundColor Green
            
            Write-Host "Language:   $($status.language)" -ForegroundColor Yellow
            Write-Host "Iterations: $($status.iterations)" -ForegroundColor Yellow
            Write-Host "Time:       $elapsed seconds`n" -ForegroundColor Yellow
            
            Write-Host "─────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
            Write-Host "ENRICHED PROMPT:" -ForegroundColor Magenta
            Write-Host "─────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
            Write-Host $status.enriched_prompt -ForegroundColor Gray
            Write-Host ""
            
            Write-Host "═════════════════════════════════════════════════════════════" -ForegroundColor Green
            Write-Host "GENERATED CODE:" -ForegroundColor Green
            Write-Host "═════════════════════════════════════════════════════════════" -ForegroundColor Green
            Write-Host $status.code -ForegroundColor White
            Write-Host "═════════════════════════════════════════════════════════════`n" -ForegroundColor Green
            
            if ($status.test_results) {
                Write-Host "Test Results:" -ForegroundColor Cyan
                $status.test_results | ConvertTo-Json -Depth 3
            }
            
            return
        }
        elseif ($status.status -eq "failed") {
            Write-Host "`n╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Red
            Write-Host "║                    ✗ FAILED                               ║" -ForegroundColor Red
            Write-Host "╚═══════════════════════════════════════════════════════════╝`n" -ForegroundColor Red
            
            Write-Host "Errors:" -ForegroundColor Red
            $status.errors | ForEach-Object { Write-Host "  • $_" -ForegroundColor Red }
            
            if ($status.code) {
                Write-Host "`nLast attempted code:" -ForegroundColor Yellow
                Write-Host $status.code -ForegroundColor Gray
            }
            return
        }
        elseif ($status.status -eq "needs_clarification") {
            Write-Host "`n╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Yellow
            Write-Host "║              ⚠ NEEDS MORE INFORMATION                     ║" -ForegroundColor Yellow
            Write-Host "╚═══════════════════════════════════════════════════════════╝`n" -ForegroundColor Yellow
            
            Write-Host "Please answer these questions:`n" -ForegroundColor Yellow
            $status.questions | ForEach-Object { Write-Host "  • $_" -ForegroundColor Cyan }
            
            Write-Host "`nTo answer, run:" -ForegroundColor Gray
            Write-Host "  `$answers = @{" -ForegroundColor DarkGray
            Write-Host "      'Question 1' = 'Your answer'" -ForegroundColor DarkGray
            Write-Host "  } | ConvertTo-Json" -ForegroundColor DarkGray
            Write-Host "  Invoke-RestMethod -Uri 'http://localhost:8000/clarify/$requestId' -Method POST -ContentType 'application/json' -Body `"`$answers`"`n" -ForegroundColor DarkGray
            return
        }
    }
    catch {
        # Silent - keep waiting
    }
}

Write-Host "`n✗ TIMEOUT after $MaxWait seconds" -ForegroundColor Red
Write-Host "Check logs: docker-compose logs agent-writer" -ForegroundColor Yellow