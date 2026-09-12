$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip install -e '.[build]'
python -m unittest discover -s tests -v
python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py

$package = 'dist/Autosport-V1'
$zip = 'dist/Autosport-V1-windows-x64.zip'
if (Test-Path $package) { Remove-Item -Recurse -Force $package }
if (Test-Path $zip) { Remove-Item -Force $zip }
New-Item -ItemType Directory -Path $package | Out-Null
Copy-Item 'dist/Autosport.exe' "$package/Autosport.exe"
Copy-Item 'WINDOWS_START_HERE.txt' "$package/WINDOWS_START_HERE.txt"
New-Item -ItemType Directory -Path "$package/examples" | Out-Null
Copy-Item -Recurse 'examples/tt_demo' "$package/examples/tt_demo"
Compress-Archive -Path "$package/*" -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Built $zip"
