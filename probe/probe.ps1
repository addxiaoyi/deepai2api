$ErrorActionPreference = 'Stop'
$cred = Get-Content "$env:USERPROFILE\.deepai-code\userdata\secrets\deepai-account.bin" -Raw | ConvertFrom-Json
$machine = 'dde678678d920d5a9c9de37c6275259d5c7ad34625c0df730445738b0be2fef2'
$inst = -join ((1..16) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) })
$headers = @{
  'X-DeepAI-Code-Theos' = '7'
  'X-DeepAI-Code-Instance' = $inst
  'X-DeepAI-Code-Machine' = $machine
  'api-key' = $cred.apiKey
  'Content-Type' = 'application/json'
  'X-DeepAI-Code-Beacon' = 'v=0.3.50;up=12;pi=1;focus=1'
}
$base = 'https://api.deepai.org/deepai_code'

Write-Output '=== POST /theos ==='
try {
  $r = Invoke-WebRequest -Uri "$base/theos" -Method POST -Headers $headers -Body '{}' -TimeoutSec 30 -UseBasicParsing
  Write-Output "STATUS $($r.StatusCode)"
  Write-Output $r.Content
} catch {
  Write-Output "ERR $($_.Exception.Message)"
  if ($_.Exception.Response) {
    $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    Write-Output $sr.ReadToEnd()
  }
}

Write-Output ''
Write-Output '=== GET /models ==='
try {
  $r2 = Invoke-WebRequest -Uri "$base/models" -Headers $headers -TimeoutSec 30 -UseBasicParsing
  Write-Output "STATUS $($r2.StatusCode)"
  Write-Output $r2.Content.Substring(0, [Math]::Min(3000, $r2.Content.Length))
} catch {
  Write-Output "ERR $($_.Exception.Message)"
  if ($_.Exception.Response) {
    $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    Write-Output $sr.ReadToEnd()
  }
}