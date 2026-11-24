$ErrorActionPreference = 'Stop'
$cred = Get-Content "$env:USERPROFILE\.deepai-code\userdata\secrets\deepai-account.bin" -Raw | ConvertFrom-Json
$machine = 'dde678678d920d5a9c9de37c6275259d5c7ad34625c0df730445738b0be2fef2'
$inst = -join ((1..16) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) })
$h = @{
  'X-DeepAI-Code-Theos' = '7'
  'X-DeepAI-Code-Instance' = $inst
  'X-DeepAI-Code-Machine' = $machine
  'api-key' = $cred.apiKey
  'Content-Type' = 'application/json'
  'X-DeepAI-Code-Beacon' = 'v=0.3.50;up=12;pi=1;focus=1'
}
$base = 'https://api.deepai.org/deepai_code'

function Show($label, $script) {
  Write-Output "=== $label ==="
  try { & $script } catch {
    Write-Output "ERR $($_.Exception.Message)"
    if ($_.Exception.Response) {
      $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
      Write-Output $sr.ReadToEnd()
    }
  }
  Write-Output ''
}

Show 'GET /theos' {
  $r = Invoke-WebRequest -Uri "$base/theos" -Headers $h -TimeoutSec 30 -UseBasicParsing
  Write-Output "STATUS $($r.StatusCode)"
  Write-Output $r.Content
}

Show 'POST /chat/completions (non-stream)' {
  $body = @{
    model = 'deepseek-v4.1-flash'
    messages = @(@{ role = 'user'; content = 'Reply with exactly: PONG' })
    stream = $false
  } | ConvertTo-Json -Depth 6 -Compress
  $r = Invoke-WebRequest -Uri "$base/chat/completions" -Method POST -Headers $h -Body $body -TimeoutSec 90 -UseBasicParsing
  Write-Output "STATUS $($r.StatusCode) CT=$($r.Headers['Content-Type'])"
  Write-Output $r.Content.Substring(0, [Math]::Min(2000, $r.Content.Length))
}