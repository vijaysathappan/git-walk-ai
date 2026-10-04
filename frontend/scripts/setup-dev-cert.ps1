$ErrorActionPreference = "Stop"

$frontendDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $frontendDir

try {
  & npm.cmd run setup:cert
  if ($LASTEXITCODE -ne 0) {
    throw "Office development certificate setup failed with exit code $LASTEXITCODE."
  }

  & npm.cmd run verify:cert
  if ($LASTEXITCODE -ne 0) {
    throw "Office development certificate verification failed with exit code $LASTEXITCODE."
  }
}
finally {
  Pop-Location
}
