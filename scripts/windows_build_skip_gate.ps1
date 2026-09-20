function ConvertTo-WindowsCandidateCoreText {
  param(
    [Parameter(Mandatory = $true)]
    [string] $CoreText
  )

  $pytestGatePattern = '(?m)^python -m pytest -v tests\r?\nif \(\$LASTEXITCODE -ne 0\) \{ throw "Full pytest gate exited \$LASTEXITCODE" \}\r?\n'
  $pytestGateRegex = [regex]::new($pytestGatePattern)
  $pytestGateMatches = $pytestGateRegex.Matches($CoreText)
  if ($pytestGateMatches.Count -ne 1) {
    throw "Candidate skip requires exactly one canonical builder-local pytest gate; observed $($pytestGateMatches.Count)"
  }

  $replacement = "Write-Host 'BUILDER_LOCAL_PYTEST=SKIPPED_BY_EXPLICIT_CALLER'`n"
  $candidateCoreText = $pytestGateRegex.Replace($CoreText, $replacement, 1)
  if ($candidateCoreText -eq $CoreText -or $candidateCoreText.Contains('python -m pytest -v tests')) {
    throw 'Candidate skip failed to remove exactly the canonical builder-local pytest invocation'
  }
  return $candidateCoreText
}

function Invoke-WindowsCandidateCoreText {
  param(
    [Parameter(Mandatory = $true)]
    [string] $CoreText
  )

  # Execute transformed builder text as a real script file, not a ScriptBlock.
  # The production builder deliberately uses $script: variables; a temporary
  # script preserves normal script scope while keeping the transformed file
  # outside the repository so exact-source checkout proofs cannot see it.
  $tempName = 'autosport-windows-candidate-' + [guid]::NewGuid().ToString('N') + '.ps1'
  $tempScript = Join-Path ([System.IO.Path]::GetTempPath()) $tempName
  try {
    [System.IO.File]::WriteAllText(
      $tempScript,
      $CoreText,
      [System.Text.UTF8Encoding]::new($false)
    )
    & $tempScript
  } finally {
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
  }
}
