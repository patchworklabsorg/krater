<#
.SYNOPSIS
One-command first-time setup for local development on Windows.

.DESCRIPTION
Installs Python 3.12 and the project's dependencies with uv, starts a Postgres 16 container for dev
and tests, creates the krater_dev and krater_test databases, and migrates krater_dev. Safe to re-run:
every step skips work that's already done (an existing container is reused and started if stopped;
existing databases are left alone).

The bash equivalent (macOS, Linux, WSL, Git Bash) is scripts/dev/setup.sh.

Optional env (defaults shown):
  KRATER_PG_CONTAINER   krater-pg
  KRATER_PG_IMAGE       postgres:16
  KRATER_PG_PORT        5432   (the host port; anything else means setting the URLs printed at the end)

Needs uv (https://docs.astral.sh/uv/) and Docker Desktop running.

.PARAMETER Check
Also run the lint, format check and test suite at the end.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\dev\setup.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Check
)

# Not "Stop": Windows PowerShell 5.1 turns any stderr output from a native command into a terminating
# error, and uv and docker both write progress to stderr. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

$PgContainer = if ($env:KRATER_PG_CONTAINER) { $env:KRATER_PG_CONTAINER } else { 'krater-pg' }
$PgImage = if ($env:KRATER_PG_IMAGE) { $env:KRATER_PG_IMAGE } else { 'postgres:16' }
$PgPort = if ($env:KRATER_PG_PORT) { $env:KRATER_PG_PORT } else { '5432' }
# root/root matches the defaults in krater/config.py and tests/conftest.py, so no env vars are needed.
$PgUser = 'root'
$PgPassword = 'root'
$Databases = @('krater_dev', 'krater_test')

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Stop-WithError([string]$Message) {
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

function Assert-LastExit([string]$What) {
    if ($LASTEXITCODE -ne 0) { Stop-WithError "$What failed (exit code $LASTEXITCODE)." }
}

# Queries go over TCP inside the container: during the image's first-boot init a temporary server
# listens on the socket only, so a socket check (or pg_isready) can pass before the real server is up.
function Invoke-PsqlAdmin {
    docker exec -e "PGPASSWORD=$PgPassword" $PgContainer `
        psql -h 127.0.0.1 -U $PgUser -d postgres -v ON_ERROR_STOP=1 @args
}

# --- uv ---------------------------------------------------------------------------------------------
# The uv installer puts uv in ~\.local\bin but can't update the PATH of the shell that ran it, so look
# there too before giving up.
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvFallback = Join-Path $HOME '.local\bin\uv.exe'
if ($uvCommand) {
    $Uv = $uvCommand.Source
} elseif (Test-Path $uvFallback) {
    $Uv = $uvFallback
} else {
    Stop-WithError ('uv not found. Install it with: ' +
        'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"')
}

Push-Location $RepoRoot
try {
    Write-Step 'Installing Python 3.12 and dependencies (uv sync)'
    & $Uv python install 3.12
    Assert-LastExit 'uv python install'
    & $Uv sync
    Assert-LastExit 'uv sync'

    # --- Postgres container -------------------------------------------------------------------------
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Stop-WithError 'docker not found. Install Docker Desktop first.'
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "the Docker daemon isn't running. Start Docker Desktop and re-run this script."
    }

    Write-Step "Postgres container '$PgContainer'"
    $state = docker inspect -f '{{.State.Status}}' $PgContainer 2> $null
    if ($LASTEXITCODE -ne 0) { $state = '' }
    if ($state -eq 'running') {
        Write-Host 'Already running.'
    } elseif ($state -eq '') {
        Write-Host "Creating it from $PgImage on host port $PgPort."
        docker run -d --name $PgContainer -e "POSTGRES_USER=$PgUser" -e "POSTGRES_PASSWORD=$PgPassword" `
            -p "${PgPort}:5432" $PgImage | Out-Null
        if ($LASTEXITCODE -ne 0) {
            # A failed run (e.g. the port is taken) still leaves a "created" container behind, and a later
            # `docker start` of it comes up without the port published. Remove it so a re-run starts clean.
            docker rm -f $PgContainer *> $null
            Stop-WithError ("couldn't start Postgres on host port $PgPort (see Docker's message above). " +
                'If the port is taken, re-run with $env:KRATER_PG_PORT set to a free one.')
        }
    } else {
        Write-Host "Exists but is '$state'; starting it."
        docker start $PgContainer | Out-Null
        Assert-LastExit 'docker start'
    }

    # Also catches a pre-existing container of the same name that publishes a different port, or none.
    $published = docker port $PgContainer 5432/tcp 2> $null
    if (-not ($published | Where-Object { "$_" -match ":$PgPort`$" })) {
        Stop-WithError ("container '$PgContainer' doesn't publish Postgres on host port $PgPort. Remove it " +
            "(docker rm -f $PgContainer) and re-run, or set KRATER_PG_CONTAINER / KRATER_PG_PORT to match it.")
    }

    Write-Host 'Waiting for Postgres to accept connections...'
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Invoke-PsqlAdmin -tAc 'SELECT 1' *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        Stop-WithError "Postgres in '$PgContainer' didn't become ready within 60s. Check: docker logs $PgContainer"
    }

    Write-Step 'Databases'
    foreach ($db in $Databases) {
        $exists = Invoke-PsqlAdmin -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'"
        Assert-LastExit "Checking for database $db"
        if ("$exists".Trim() -eq '1') {
            Write-Host "${db}: already exists."
        } else {
            Invoke-PsqlAdmin -qc "CREATE DATABASE $db"
            Assert-LastExit "Creating database $db"
            Write-Host "${db}: created."
        }
    }

    $DevUrl = "postgresql+psycopg://${PgUser}:${PgPassword}@localhost:${PgPort}/krater_dev"
    $TestUrl = "postgresql+psycopg://${PgUser}:${PgPassword}@localhost:${PgPort}/krater_test"

    Write-Step 'Migrating krater_dev (alembic upgrade head)'
    $savedDbUrl = $env:KRATER_DATABASE_URL
    $env:KRATER_DATABASE_URL = $DevUrl
    try {
        & $Uv run alembic upgrade head
        Assert-LastExit 'alembic upgrade head'
    } finally {
        $env:KRATER_DATABASE_URL = $savedDbUrl
    }

    # --- Checks -------------------------------------------------------------------------------------
    if (Test-Path (Join-Path $RepoRoot '.env')) {
        Write-Host ''
        Write-Host ("warning: $RepoRoot\.env exists. pydantic-settings loads it automatically, so its " +
            'settings leak into the test suite. Rename it (e.g. .env.local) and load it into your shell ' +
            'only when running the app.') -ForegroundColor Yellow
    }

    if ($Check) {
        Write-Step 'Lint, format check and tests'
        & $Uv run ruff check .
        Assert-LastExit 'ruff check'
        & $Uv run ruff format --check .
        Assert-LastExit 'ruff format --check'
        $savedTestUrl = $env:KRATER_TEST_DATABASE_URL
        $env:KRATER_TEST_DATABASE_URL = $TestUrl
        try {
            & $Uv run pytest
            Assert-LastExit 'pytest'
        } finally {
            $env:KRATER_TEST_DATABASE_URL = $savedTestUrl
        }
    }

    Write-Step 'Done'
    if ($PgPort -ne '5432') {
        Write-Host "Postgres is on port $PgPort, not the default, so set these first:"
        Write-Host "  `$env:KRATER_DATABASE_URL = `"$DevUrl`""
        Write-Host "  `$env:KRATER_TEST_DATABASE_URL = `"$TestUrl`""
    }
    Write-Host 'Run the app in stub mode (fake users, fake SkyPilot/Slack/S3), then open http://localhost:8000:'
    Write-Host '  uv run uvicorn krater.web.app:create_app --factory --reload'
    Write-Host 'Tests and lint:'
    Write-Host '  uv run pytest'
    Write-Host '  uv run ruff check . ; uv run ruff format --check .'
} finally {
    Pop-Location
}
