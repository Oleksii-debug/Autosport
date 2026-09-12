$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip install -e '.[build]'
python -m unittest discover -s tests -v
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py

$diag = Join-Path $PWD 'dist/packaged-diagnostic.json'
if (Test-Path $diag) { Remove-Item -Force $diag }
$process = Start-Process -FilePath (Join-Path $PWD 'dist/Autosport.exe') -ArgumentList '--diagnostic-output', $diag -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Packaged Autosport.exe diagnostic exited $($process.ExitCode)" }
$diagnostic = Get-Content $diag -Raw | ConvertFrom-Json
if ($diagnostic.status -ne 'PASS') { throw 'Packaged Autosport.exe diagnostic did not PASS' }

$sourceSha = $env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) { $sourceSha = (git rev-parse HEAD).Trim() }
python scripts/package_windows.py `
  --exe dist/Autosport.exe `
  --start-file WINDOWS_START_HERE.txt `
  --example-dir examples/tt_demo `
  --diagnostic $diag `
  --output dist/Autosport-V1-windows-x64.zip `
  --source-sha $sourceSha
