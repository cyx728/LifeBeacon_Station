$ErrorActionPreference = 'Stop'
$radioRoot = 'D:\ProgramData\radioconda'
$env:PATH = "$radioRoot;$radioRoot\Library\bin;$radioRoot\Scripts;$env:PATH"
& "$radioRoot\python.exe" -u "$PSScriptRoot\receiver.py" @args
if ($LASTEXITCODE -ne 0) { throw "LoRa receiver exited with code $LASTEXITCODE" }
