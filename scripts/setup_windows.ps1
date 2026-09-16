# Requires Windows PowerShell 5.1 or PowerShell 7 on Windows.
[CmdletBinding()]
param([switch]$RunTests)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$script:UvExecutable = $null

function Invoke-Uv {
    param([string[]]$UvArguments)
    & $script:UvExecutable @UvArguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv failed (exit $LASTEXITCODE): $($UvArguments -join ' ')"
    }
}

$exitCode = 0
$locationChanged = $false
$savedEnvironment = @{}
foreach ($name in @('UV_PROJECT_ENVIRONMENT', 'UV_INSTALL_DIR', 'UV_NO_MODIFY_PATH', 'QT_QPA_PLATFORM')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    if ($env:OS -ne 'Windows_NT') { throw 'This script requires Windows.' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw '64-bit Windows is required.' }
    foreach ($relativePath in @('pyproject.toml', 'uv.lock', '.python-version', 'curry8-netstream/pyproject.toml')) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath) -PathType Leaf)) {
            throw "Incomplete project: missing $relativePath. Download or clone the whole repository."
        }
    }
    $venvPath = Join-Path $projectRoot '.venv'
    if ((Test-Path -LiteralPath $venvPath) -and
        -not (Test-Path -LiteralPath (Join-Path $venvPath 'Scripts/python.exe'))) {
        throw 'Existing .venv is not a Windows environment. Move it aside manually and retry; it was not deleted.'
    }
    Push-Location -LiteralPath $projectRoot
    $locationChanged = $true
    $env:UV_PROJECT_ENVIRONMENT = $venvPath

    $uvCommand = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $localUv = Join-Path $env:LOCALAPPDATA 'sleep-stim-controller/uv-bin/uv.exe'
    if ($null -ne $uvCommand) {
        $script:UvExecutable = $uvCommand.Source
    } elseif (Test-Path -LiteralPath $localUv -PathType Leaf) {
        $script:UvExecutable = $localUv
    } else {
        Write-Host 'Installing uv 0.12.1 from astral.sh for this user...'
        $installerPath = Join-Path ([IO.Path]::GetTempPath()) ("sleep-uv-" + [guid]::NewGuid().ToString('N') + '.ps1')
        $savedTls = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = $savedTls -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.12.1/install.ps1' -OutFile $installerPath
            $env:UV_INSTALL_DIR = Split-Path -Parent $localUv
            $env:UV_NO_MODIFY_PATH = '1'
            $powershellExe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
            & $powershellExe -NoProfile -ExecutionPolicy Bypass -File $installerPath
            if ($LASTEXITCODE -ne 0) { throw "uv installer failed (exit $LASTEXITCODE)." }
        } finally {
            [Net.ServicePointManager]::SecurityProtocol = $savedTls
            if (Test-Path -LiteralPath $installerPath) { Remove-Item -LiteralPath $installerPath -Force }
        }
        if (-not (Test-Path -LiteralPath $localUv -PathType Leaf)) { throw "uv.exe not found at $localUv" }
        $script:UvExecutable = $localUv
    }

    Invoke-Uv -UvArguments @('--version')
    $pythonVersion = (Get-Content -LiteralPath (Join-Path $projectRoot '.python-version') -Raw).Trim()
    if ($pythonVersion -notmatch '^3\.11(\.\d+)?$') { throw 'This project requires Python 3.11.' }
    Invoke-Uv -UvArguments @('python', 'install', $pythonVersion)
    Invoke-Uv -UvArguments @('sync', '--locked', '--python', $pythonVersion)

    $probe = @'
import importlib.util, sys
import sleep_stim_controller, curry_netstream, numpy, PySide6
assert sys.version_info[:2] == (3, 11), sys.version
if importlib.util.find_spec('sleep_stim_controller.onnx_staging') is not None:
    import onnxruntime, scipy
print('Environment imports OK:', sys.executable)
'@
    Invoke-Uv -UvArguments @('run', '--locked', '--no-sync', 'python', '-c', $probe)
    if ($RunTests) {
        $env:QT_QPA_PLATFORM = 'offscreen'
        Invoke-Uv -UvArguments @('run', '--locked', '--no-sync', 'pytest')
    }
    Write-Host ''
    Write-Host 'Setup complete. From the project root, start the application with:'
    Write-Host '  .\.venv\Scripts\sleep-stim-controller.exe'
    Write-Host 'The application and devices were not started. NoModel remains the default.'
} catch {
    [Console]::Error.WriteLine("Setup failed: " + $_.Exception.Message)
    $exitCode = 1
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if ($locationChanged) { Pop-Location }
}
exit $exitCode
