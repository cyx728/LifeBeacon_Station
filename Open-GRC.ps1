$ErrorActionPreference = 'Stop'
$radioRoot = 'D:\ProgramData\radioconda'
$env:PATH = "$radioRoot;$radioRoot\Library\bin;$radioRoot\Scripts;$env:PATH"
Start-Process -FilePath "$radioRoot\pythonw.exe" -ArgumentList ('"' + "$radioRoot\Library\bin\gnuradio-companion.exe" + '" "' + "$PSScriptRoot\RTL_SDR_LoRa_RX.grc" + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
