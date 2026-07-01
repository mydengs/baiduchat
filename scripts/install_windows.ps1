param(
  [int]$Port = 0,
  [string]$AdminPassword = "",
  [string]$HostAddress = "127.0.0.1",
  [string]$TaskName = "",
  [switch]$NoStartup,
  [switch]$NoStart,
  [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
  Write-Host "Usage: .\scripts\install_windows.bat [-Port 8000] [-AdminPassword password] [-HostAddress 127.0.0.1] [-TaskName BaiduOpenAIProxy-8000] [-NoStartup] [-NoStart]"
  exit 0
}

function Test-IsAdmin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = [Security.Principal.WindowsPrincipal]::new($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-Python {
  $candidates = @("py", "python")
  foreach ($candidate in $candidates) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) {
      continue
    }
    try {
      if ($candidate -eq "py") {
        & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) {
          return @{ Command = "py"; Args = @("-3") }
        }
      } else {
        & python -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) {
          return @{ Command = "python"; Args = @() }
        }
      }
    } catch {
      continue
    }
  }
  throw "Python 3 was not found. Install Python 3.11+ and enable 'Add python.exe to PATH', then run this script again."
}

function Invoke-Python {
  param(
    [hashtable]$Python,
    [string[]]$Arguments
  )
  & $Python.Command @($Python.Args + $Arguments)
  if ($LASTEXITCODE -ne 0) {
    throw "Python command failed: $($Arguments -join ' ')"
  }
}

function Set-EnvValue {
  param(
    [string]$Path,
    [string]$Key,
    [string]$Value
  )
  $lines = @()
  if (Test-Path $Path) {
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
      [void]$lines.Add($line)
    }
  } else {
    $lines = [System.Collections.Generic.List[string]]::new()
  }

  $updated = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i].StartsWith("$Key=")) {
      $lines[$i] = "$Key=$Value"
      $updated = $true
      break
    }
  }
  if (-not $updated) {
    [void]$lines.Add("$Key=$Value")
  }
  Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

function New-Secret {
  $bytes = New-Object byte[] 48
  [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
  return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $AppDir

if ($Port -le 0) {
  $rawPort = Read-Host "Project port [8000]"
  if ([string]::IsNullOrWhiteSpace($rawPort)) {
    $Port = 8000
  } else {
    $Port = [int]$rawPort
  }
}

if ($Port -lt 1 -or $Port -gt 65535) {
  throw "Port must be between 1 and 65535."
}

if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
  $secure = Read-Host "Admin password" -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    $AdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
  throw "Admin password is required."
}

if ([string]::IsNullOrWhiteSpace($TaskName)) {
  $TaskName = "BaiduOpenAIProxy-$Port"
}

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener -and -not $NoStart) {
  throw "Port $Port is already in use. Choose another port for a second instance, or stop the existing process first."
}

New-Item -ItemType Directory -Force -Path "data", "logs" | Out-Null

$python = Resolve-Python
if (-not (Test-Path ".venv")) {
  Invoke-Python -Python $python -Arguments @("-m", "venv", ".venv")
}

$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
  throw "Virtual environment Python was not created at $VenvPython"
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

& $VenvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "dependency install failed." }

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
}

Set-EnvValue -Path ".env" -Key "APP_HOST" -Value $HostAddress
Set-EnvValue -Path ".env" -Key "APP_PORT" -Value ([string]$Port)
Set-EnvValue -Path ".env" -Key "ADMIN_PASSWORD" -Value $AdminPassword
Set-EnvValue -Path ".env" -Key "APP_SECRET" -Value (New-Secret)

$env:INSTALL_ADMIN_PASSWORD = $AdminPassword
& $VenvPython -c "import os; from app.db.init_db import init_db; key = init_db(admin_password=os.environ['INSTALL_ADMIN_PASSWORD']); print(('Created default API key: ' + key) if key else 'Database initialized.')"
if ($LASTEXITCODE -ne 0) { throw "database initialization failed." }
Remove-Item Env:\INSTALL_ADMIN_PASSWORD -ErrorAction SilentlyContinue

$StartArgs = @("-m", "uvicorn", "app.main:app", "--host", $HostAddress, "--port", [string]$Port)

if (-not $NoStartup) {
  $action = New-ScheduledTaskAction -Execute $VenvPython -Argument ($StartArgs -join " ") -WorkingDirectory $AppDir
  if (Test-IsAdmin) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
    $taskMode = "startup task as SYSTEM"
  } else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $taskMode = "logon task for current user"
    Write-Host "Not running as Administrator; creating a logon task instead of a machine startup task."
  }
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Baidu OpenAI Proxy ($Port)" -Force | Out-Null
  Write-Host "Created ${taskMode}: $TaskName"
}

if (-not $NoStart) {
  if (-not $NoStartup) {
    Start-ScheduledTask -TaskName $TaskName
  } else {
    $out = Join-Path $AppDir "logs\windows.$Port.out.log"
    $err = Join-Path $AppDir "logs\windows.$Port.err.log"
    Start-Process -FilePath $VenvPython -ArgumentList $StartArgs -WorkingDirectory $AppDir -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
  }
}

Write-Host ""
Write-Host "Windows deployment completed."
Write-Host "App directory: $AppDir"
Write-Host "Port: $Port"
Write-Host "Startup task: $(if ($NoStartup) { 'disabled' } else { $TaskName })"
Write-Host "Admin: http://${HostAddress}:$Port/admin"
