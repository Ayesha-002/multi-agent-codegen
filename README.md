# 🤖 Multi-Agent Code Generation System

A production-ready AI-powered code generation pipeline that automatically writes, verifies, tests, and improves code using local LLMs — **no paid API key required**.

---

## 📋 Table of Contents

- [System Overview](#system-overview)
- [Architecture](#architecture)
- [Hardware Requirements](#hardware-requirements)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Installation & Setup](#installation--setup)
- [Configuration](#configuration)
- [Running the System](#running-the-system)
- [API Reference](#api-reference)
- [PowerShell Commands](#powershell-commands)
- [Workflow Explained](#workflow-explained)
- [Troubleshooting](#troubleshooting)
- [Upgrading to Claude API](#upgrading-to-claude-api)

---

## System Overview

This system accepts a natural language prompt from a user (even vague ones), intelligently analyzes it, generates code, verifies it for errors, runs tests, and automatically improves it if needed — all in a loop until the code passes or a maximum iteration limit is reached.

**Key feature:** You do not need to write perfect prompts. The Analyzer Agent understands vague requests and either infers missing details (language, requirements) or asks the user targeted clarifying questions before generating code.

---

## Architecture

```
User Prompt
     │
     ▼
┌─────────────────┐
│ Coordinator API │  ← FastAPI on port 8000
│   (main.py)     │
└────────┬────────┘
         │ publishes to RabbitMQ
         ▼
┌─────────────────┐
│ Analyzer Agent  │  ← Understands vague prompts, infers language,
│                 │    enriches prompt, or asks clarifying questions
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Writer Agent   │  ← Generates code using DeepSeek-Coder 6.7B (local)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Verifier Agent  │  ← Reviews code for bugs, security issues, syntax errors
└────────┬────────┘
    ┌────┴────┐
    │         │
  Pass       Fail
    │         │
    ▼         ▼
┌────────┐ ┌──────────────┐
│ Tester │ │ Improver     │  ← Fixes issues and loops back to Verifier
└────┬───┘ └──────────────┘
  Pass/Fail
    │
    ▼
 Result stored in Redis
 User polls /status/{id}
```

**Infrastructure:**
- **RabbitMQ** — Message bus between agents (task queues)
- **Redis** — Stores workflow state per request (1 hour TTL)
- **Ollama** — Runs DeepSeek-Coder 6.7B locally on your machine
- **Docker Compose** — Isolates each agent in its own container

---

## Hardware Requirements

| Component | Minimum | Your Setup |
|-----------|---------|------------|
| CPU | Quad-core | Intel i7 8th Gen ✅ |
| RAM | 16 GB | 16 GB ✅ |
| Storage | 20 GB free | 268 GB free ✅ |
| OS | Windows 10/11 | Windows 11 ✅ |

**RAM allocation at runtime:**

| Service | RAM Used |
|---------|----------|
| Windows OS | ~3.5 GB |
| Docker Desktop | ~800 MB |
| Ollama (DeepSeek 6.7B) | ~4 GB |
| All 5 Agents + Coordinator | ~5 GB |
| RabbitMQ + Redis | ~300 MB |
| **Buffer remaining** | **~2.4 GB** |

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| API | FastAPI + Uvicorn | REST API coordinator |
| Agents | Python 3.11 | Individual agent logic |
| LLM (local) | Ollama + DeepSeek-Coder 6.7B | Code generation (free) |
| LLM (optional) | Claude API (Anthropic) | Higher quality (paid) |
| Message Queue | RabbitMQ 3.12 | Agent communication |
| State Store | Redis 7 | Workflow state |
| Containers | Docker Compose | Service isolation |
| Logging | structlog (JSON) | Structured log output |

---

## Project Structure

```
multi-agent-codegen/
│
├── docker-compose.yml          ← Defines all services and networking
├── .env                        ← Secrets and configuration (never commit this)
│
├── coordinator/
│   ├── main.py                 ← FastAPI app: endpoints, workflow orchestration
│   ├── Dockerfile
│   └── requirements.txt
│
├── agents/
│   ├── analyzer/               ← NEW: Understands and enriches user prompts
│   │   ├── agent.py
│   │   ├── utils.py            ← RabbitMQ connection + retry logic
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── writer/                 ← Generates code using local LLM
│   │   ├── agent.py
│   │   ├── utils.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── verifier/               ← Reviews code for errors using local LLM
│   │   ├── agent.py
│   │   ├── utils.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   ├── tester/                 ← Runs syntax and basic code tests
│   │   ├── agent.py
│   │   ├── utils.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   └── improver/               ← Fixes code issues and loops back to verifier
│       ├── agent.py
│       ├── utils.py
│       ├── Dockerfile
│       └── requirements.txt
│
├── logs/                       ← JSON logs from all agents
└── tests/                      ← Generated test artifacts
```

---

## Installation & Setup

### Step 1: Install Prerequisites

**Docker Desktop**
```
Download: https://www.docker.com/products/docker-desktop/
- Enable WSL 2 backend in Docker Desktop Settings
- Allocate: CPUs=6, RAM=12GB, Swap=2GB
```

**Ollama (local LLM runner)**
```
Download: https://ollama.com/download/windows
```

**Pull the AI model (run once, downloads ~4GB)**
```bash
ollama pull deepseek-coder:6.7b-instruct-q4_K_M
```

**Verify the model works**
```bash
ollama run deepseek-coder:6.7b-instruct-q4_K_M "Write hello world in Python"
```

### Step 2: Clone / Create Project

```bash
mkdir multi-agent-codegen
cd multi-agent-codegen

# Create all required directories
mkdir -p coordinator
mkdir -p agents\analyzer agents\writer agents\verifier agents\tester agents\improver
mkdir -p logs tests
```

### Step 3: Create Python Virtual Environment

```bash
python -m venv venv
venv\Scripts\activate
pip install -r coordinator\requirements.txt
```

### Step 4: Configure Environment

Create `.env` file in project root:
```bash
# Your actual passwords (change these)
RABBITMQ_PASSWORD=your_rabbitmq_password_here
REDIS_PASSWORD=your_redis_password_here

# Optional: only needed if switching to Claude API later
ANTHROPIC_API_KEY=sk-ant-your-key-here

# LLM settings
OLLAMA_HOST=http://host.docker.internal:11434
MODEL_NAME=deepseek-coder:6.7b-instruct-q4_K_M

# Workflow
MAX_ITERATIONS=5
```

> ⚠️ **Never commit `.env` to Git.** Add it to `.gitignore`.

### Step 5: Sync utils.py to All Agents

```powershell
# Run in PowerShell from project root
# utils.py contains RabbitMQ retry logic shared by all agents
Copy-Item "agents\analyzer\utils.py" "agents\writer\utils.py"
Copy-Item "agents\analyzer\utils.py" "agents\verifier\utils.py"
Copy-Item "agents\analyzer\utils.py" "agents\tester\utils.py"
Copy-Item "agents\analyzer\utils.py" "agents\improver\utils.py"
```

**Verify all utils.py files are correct:**
```powershell
foreach ($agent in @("analyzer","writer","verifier","tester","improver")) {
    $content = Get-Content "agents\$agent\utils.py" -Raw
    if ($content -match "def connect_rabbitmq" -and $content -match "def reconnect_on_failure") {
        Write-Host "OK  agents\$agent\utils.py" -ForegroundColor Green
    } else {
        Write-Host "BROKEN  agents\$agent\utils.py" -ForegroundColor Red
    }
}
```

---

## Configuration

### `.env` File Reference

```bash
RABBITMQ_PASSWORD=         # RabbitMQ broker password
REDIS_PASSWORD=            # Redis password
ANTHROPIC_API_KEY=         # Claude API key (optional, for paid upgrade)
OLLAMA_HOST=               # Ollama server URL (default: host.docker.internal:11434)
MODEL_NAME=                # Which Ollama model to use
MAX_ITERATIONS=5           # Max improvement cycles before giving up
```

### Supported Models (choose by RAM)

| Model | RAM Required | Quality | Speed |
|-------|-------------|---------|-------|
| `deepseek-coder:1.3b-instruct` | ~1 GB | Basic | Very Fast |
| `deepseek-coder:6.7b-instruct-q4_K_M` | ~4 GB | **Good ✅ Recommended** | Medium |
| `codellama:7b-instruct-q4_K_M` | ~4 GB | Good | Medium |
| `deepseek-coder:33b-instruct-q4_K_M` | ~19 GB | Excellent | Slow |

> ⚠️ The 33B model requires more RAM than available (19 GB needed vs 16 GB available). Use 6.7B.

---

## Running the System

### Start Everything

```bash
# Build images and start all services in background
docker-compose up --build -d
```

### Stop Everything

```bash
# Stop all containers (keeps data)
docker-compose down

# Stop and delete all data (volumes)
docker-compose down -v
```

### Restart a Single Agent (after code changes)

```bash
# Rebuild and restart only the analyzer agent
docker-compose up --build -d agent-analyzer

# Restart without rebuilding
docker-compose restart agent-writer
```

### Check All Services Are Running

```bash
docker-compose ps
```

Expected output — all should show `Up`:
```
NAME                     STATUS
codegen-agent-analyzer   Up
codegen-agent-writer     Up
codegen-agent-verifier   Up
codegen-agent-tester     Up
codegen-agent-improver   Up
codegen-coordinator      Up
codegen-rabbitmq         Up (healthy)
codegen-redis            Up (healthy)
```

### View Live Logs

```bash
# All agents at once (most useful for debugging)
docker-compose logs -f

# Specific service
docker-compose logs -f agent-writer
docker-compose logs -f coordinator

# Last 50 lines of coordinator
docker-compose logs --tail=50 coordinator
```

### Monitor Resource Usage

```bash
# Live CPU and RAM per container
docker stats
```

### Check Ollama is Running

```bash
# List loaded models
ollama ps

# List downloaded models
ollama list
```

---

## API Reference

Base URL: `http://localhost:8000`

### `GET /health`
Check if the coordinator is running.

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/health"
```

Response:
```json
{"status": "healthy", "service": "coordinator", "version": "2.0.0"}
```

---

### `POST /generate`
Submit a code generation request. Prompt can be vague — the system will figure it out.

**Request body:**
```json
{
  "prompt": "your request here (can be vague)",
  "language": "python",        // optional — system infers if not provided
  "max_iterations": 5,         // optional — default 5
  "requirements": ["list", "of", "extra", "requirements"]  // optional
}
```

**PowerShell examples:**

```powershell
# Simple request
$body = '{"prompt": "write a function to reverse a string"}'
$r = Invoke-RestMethod -Uri "http://localhost:8000/generate" -Method POST -ContentType "application/json" -Body $body
Write-Host "Request ID: $($r.request_id)"
```

```powershell
# With language and requirements
$body = '{"prompt": "REST API client for weather data", "language": "python", "requirements": ["Use requests library", "Handle errors", "Add retry logic"]}'
$r = Invoke-RestMethod -Uri "http://localhost:8000/generate" -Method POST -ContentType "application/json" -Body $body
Write-Host "Request ID: $($r.request_id)"
```

```powershell
# Vague prompt — system will infer details
$body = '{"prompt": "make something that reads a csv and shows stats"}'
$r = Invoke-RestMethod -Uri "http://localhost:8000/generate" -Method POST -ContentType "application/json" -Body $body
Write-Host "Request ID: $($r.request_id)"
```

**Response:**
```json
{
  "request_id": "046a8626-3698-4d09-ac58-0eebf8030c40",
  "status": "processing",
  "message": "Your request is being analyzed..."
}
```

---

### `GET /status/{request_id}`
Poll this endpoint to get results. Keep polling until status is `completed` or `failed`.

```powershell
# Single check
Invoke-RestMethod -Uri "http://localhost:8000/status/YOUR_REQUEST_ID_HERE"
```

```powershell
# Auto-poll every 15 seconds until done
$requestId = "YOUR_REQUEST_ID_HERE"
while ($true) {
    $s = Invoke-RestMethod -Uri "http://localhost:8000/status/$requestId"
    Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] Status: $($s.status) | Stage: $($s.status) | Iterations: $($s.iterations)"

    if ($s.status -eq "completed") {
        Write-Host "`nDONE! Language: $($s.language)" -ForegroundColor Green
        Write-Host "Enriched Prompt: $($s.enriched_prompt)" -ForegroundColor Yellow
        Write-Host "`n===== GENERATED CODE =====" -ForegroundColor Cyan
        Write-Host $s.code
        break
    }
    elseif ($s.status -eq "failed") {
        Write-Host "FAILED after $($s.iterations) iterations" -ForegroundColor Red
        Write-Host "Errors: $($s.errors)"
        break
    }
    elseif ($s.status -eq "needs_clarification") {
        Write-Host "`nNEEDS CLARIFICATION:" -ForegroundColor Yellow
        $s.questions | ForEach-Object { Write-Host "  - $_" }
        break
    }
    Start-Sleep -Seconds 15
}
```

**Possible status values:**

| Status | Meaning |
|--------|---------|
| `processing` | Still running through the pipeline |
| `needs_clarification` | Agent needs more info from user |
| `completed` | Code generated and tested successfully |
| `failed` | Could not complete after max iterations |

**Completed response includes:**
```json
{
  "request_id": "...",
  "status": "completed",
  "code": "def sort_list(lst):\n    return sorted(lst)",
  "language": "python",
  "iterations": 1,
  "enriched_prompt": "Write a Python function that sorts a list...",
  "test_results": {"total": 1, "passed": 1, "failed": 0}
}
```

---

### `POST /clarify/{request_id}`
Answer clarifying questions when status is `needs_clarification`.

```powershell
# First check what questions were asked
$s = Invoke-RestMethod -Uri "http://localhost:8000/status/YOUR_REQUEST_ID"
$s.questions   # prints the list of questions

# Then submit answers
$answers = '{"answers": {"What programming language should be used?": "Python", "What should the function return?": "A sorted list"}}'
Invoke-RestMethod -Uri "http://localhost:8000/clarify/YOUR_REQUEST_ID" -Method POST -ContentType "application/json" -Body $answers
```

---

### `GET /history`
List the last 20 requests.

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/history" | ConvertTo-Json -Depth 5
```

---

### `GET /docs`
Interactive API documentation (Swagger UI) — open in browser:
```
http://localhost:8000/docs
```

---

## PowerShell Commands

### Quick Reference

```powershell
# --- SUBMIT A REQUEST ---
$body = '{"prompt": "YOUR PROMPT HERE"}'
$r = Invoke-RestMethod -Uri "http://localhost:8000/generate" -Method POST -ContentType "application/json" -Body $body
Write-Host "ID: $($r.request_id)"

# --- CHECK STATUS ---
Invoke-RestMethod -Uri "http://localhost:8000/status/$($r.request_id)"

# --- GET ONLY THE CODE ---
$result = Invoke-RestMethod -Uri "http://localhost:8000/status/$($r.request_id)"
Write-Host $result.code

# --- CHECK HEALTH ---
Invoke-RestMethod -Uri "http://localhost:8000/health"

# --- VIEW HISTORY ---
Invoke-RestMethod -Uri "http://localhost:8000/history" | ConvertTo-Json -Depth 3

# --- SUBMIT CLARIFICATION ---
$answers = '{"answers": {"question here": "answer here"}}'
Invoke-RestMethod -Uri "http://localhost:8000/clarify/REQUEST_ID" -Method POST -ContentType "application/json" -Body $answers
```

### Docker Commands

```powershell
# Start all services
docker-compose up -d

# Start with rebuild (after code changes)
docker-compose up --build -d

# Stop all services
docker-compose down

# Stop and wipe all data
docker-compose down -v

# View logs (all agents)
docker-compose logs -f

# View logs for one service
docker-compose logs -f agent-writer

# Check container status
docker-compose ps

# Restart one agent
docker-compose restart agent-analyzer

# Rebuild one agent only
docker-compose up --build -d agent-analyzer

# See resource usage
docker stats
```

### Ollama Commands

```powershell
# Download the recommended model
ollama pull deepseek-coder:6.7b-instruct-q4_K_M

# List downloaded models
ollama list

# Test the model manually
ollama run deepseek-coder:6.7b-instruct-q4_K_M "Write a Python hello world"

# Check if model is loaded in memory
ollama ps

# Remove a model
ollama rm deepseek-coder:33b-instruct-q4_K_M
```

---

## Workflow Explained

### Pipeline Stages

```
1. ANALYZER  → Reads user prompt
               - Detects language (from keywords like "python", "go", "javascript")
               - Infers missing details using LLM
               - Enriches vague prompts into detailed instructions
               - If truly unclear → sets status to needs_clarification

2. WRITER    → Receives enriched prompt
               - Sends to DeepSeek-Coder via Ollama HTTP API
               - Strips markdown fences from response
               - Sends raw code to verifier

3. VERIFIER  → Receives code
               - Asks LLM to check for syntax errors, bugs, security issues
               - Returns severity: critical/high/medium/low/none
               - If severity is low or none → passes to tester
               - If severity is high/critical → sends to improver

4. TESTER    → Receives verified code
               - For Python: runs compile() to catch SyntaxErrors
               - Other languages: basic structural check
               - Pass → marks workflow as completed
               - Fail → sends to improver with error details

5. IMPROVER  → Receives code + list of issues
               - Asks LLM to fix all listed issues
               - Returns fixed code
               - Sends back to verifier
               - Checks iteration count — stops at max_iterations

6. COMPLETED → Final code stored in Redis
               - User polls /status/{id} to retrieve it
```

### Iteration Control

```
Max iterations = 5 (configurable in .env)

Writer (iteration 1)
  → Verifier → Tester → DONE ✅           (1 iteration, no issues)
  → Verifier → Improver → Verifier → Tester → DONE ✅  (2 iterations)
  → ... up to 5 iterations
  → After 5: status = failed, returns best code with error list
```

### Message Flow (RabbitMQ Queues)

```
analyzer    ← coordinator publishes here first
code_writer ← analyzer publishes here when prompt is ready
verifier    ← writer publishes here after generating code
tester      ← verifier publishes here if no critical errors
improver    ← verifier/tester publishes here if errors found
```

---

## Troubleshooting

### "utils.py: cannot import connect_rabbitmq"
The utils.py file is empty or has wrong content.
```powershell
# Fix by overwriting with correct content then rebuild
Copy-Item "agents\analyzer\utils.py" "agents\writer\utils.py"
Copy-Item "agents\analyzer\utils.py" "agents\verifier\utils.py"
Copy-Item "agents\analyzer\utils.py" "agents\tester\utils.py"
Copy-Item "agents\analyzer\utils.py" "agents\improver\utils.py"
docker-compose up --build -d
```

### "Dockerfile cannot be empty"
An agent's Dockerfile was created as empty placeholder.
```powershell
# Check which files are empty
Get-ChildItem -Recurse -Filter "Dockerfile" | Where-Object { $_.Length -eq 0 }
# Re-create the empty ones with correct content
```

### "pika.exceptions.AMQPConnectionError"
Agent started before RabbitMQ was ready. The retry logic handles this automatically — wait 30-60 seconds and check logs again.
```bash
docker-compose logs -f agent-writer   # Should show "rabbitmq_connected" within 60s
```

### "missed heartbeats from client, timeout: 60s"
LLM call took too long and RabbitMQ dropped the connection.
Fixed by `heartbeat=0` in utils.py (disables timeout). Ensure your utils.py has this line:
```python
params.heartbeat = 0
```

### "model requires more system memory (19.4 GiB)"
You tried to run the 33B model. Use the 6.7B model instead:
```bash
ollama pull deepseek-coder:6.7b-instruct-q4_K_M
```
Update MODEL_NAME in `.env` to `deepseek-coder:6.7b-instruct-q4_K_M`.

### Status stays "processing" forever
```bash
# Check all agents are running
docker-compose ps

# Check for errors in agents
docker-compose logs --tail=30 agent-analyzer agent-writer

# Check Ollama is running and model is available
ollama ps
ollama list
```

### "invalid x-api-key" in verifier/improver logs
Old agent code still references Claude API. Rebuild those agents:
```bash
docker-compose up --build -d agent-verifier agent-improver
```

### Redis connection refused
Redis password in `.env` doesn't match. Check:
```bash
docker-compose logs redis
```
Make sure `REDIS_PASSWORD` in `.env` matches what you set.

---

## Upgrading to Claude API

When you're ready to use Claude for higher quality verification and improvement:

**1. Update `.env`:**
```bash
ANTHROPIC_API_KEY=sk-ant-your-real-key-here
```

**2. Update `agents/verifier/requirements.txt`:**
```
pika==1.3.2
redis==5.0.1
structlog==24.1.0
anthropic==0.40.0
```

**3. Update `agents/verifier/agent.py`** — replace the `verify_code` method to use:
```python
from anthropic import Anthropic
self.anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
response = self.anthropic.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": prompt}]
)
```

**4. Rebuild:**
```bash
docker-compose up --build -d agent-verifier agent-improver
```

**Cost estimate:** ~$0.02–$0.10 per code generation workflow with Claude Sonnet.

---

## RabbitMQ Management UI

Access the RabbitMQ web dashboard to see queues, message rates, and connections:

```
URL:      http://localhost:15672
Username: codegen
Password: (your RABBITMQ_PASSWORD from .env)
```

Use this to:
- See how many messages are queued
- Check if agents are consuming messages
- Purge stuck queues during debugging

---

## Version History

| Version | Changes |
|---------|---------|
| v1.0 | Initial setup: Writer, Verifier, Tester, Improver agents |
| v1.1 | Fixed empty Dockerfile errors |
| v1.2 | Fixed RabbitMQ heartbeat timeout (added heartbeat=0) |
| v1.3 | Added utils.py with retry/reconnect logic to all agents |
| v1.4 | Switched Verifier and Improver from Claude API to local Ollama |
| v2.0 | Added Analyzer Agent with prompt enrichment and clarification questions |

---

*Built with DeepSeek-Coder, Ollama, RabbitMQ, Redis, FastAPI, and Docker.*