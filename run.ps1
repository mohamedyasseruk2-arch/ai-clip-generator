# Starts the AI Clip Generator in your browser.  Usage:  .\run.ps1
Set-Location $PSScriptRoot

# pick up tools installed by winget (ffmpeg, deno) even in terminals opened before installing them
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')

python -c "import streamlit" 2>$null
if (-not $?) {
    Write-Host "Installing requirements (first run only)..."
    python -m pip install -r requirements.txt
}
# skip Streamlit's one-time "enter your email" prompt (it hangs/crashes when there's no keyboard input)
$cred = Join-Path $HOME '.streamlit\credentials.toml'
if (-not (Test-Path $cred)) {
    New-Item -ItemType Directory -Force (Split-Path $cred) | Out-Null
    Set-Content -Path $cred -Value "[general]`nemail = `"`"" -Encoding ascii
}

python -m streamlit run app.py
