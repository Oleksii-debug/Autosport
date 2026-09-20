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
