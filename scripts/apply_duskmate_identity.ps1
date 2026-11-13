param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$oldId = 'plugin.video.themoviedb.helper'
$newId = 'plugin.video.duskmate'
$scriptPath = $MyInvocation.MyCommand.Path
$files = & rg -l --hidden --glob '!.git/**' --fixed-strings $oldId $Root

foreach ($file in $files) {
    $resolved = (Resolve-Path -LiteralPath $file).Path
    if ($resolved -eq $scriptPath) { continue }
    $content = [IO.File]::ReadAllText($resolved)
    $updated = $content.Replace($oldId, $newId)
    if ($updated -ne $content) {
        [IO.File]::WriteAllText($resolved, $updated, [Text.UTF8Encoding]::new($false))
    }
}
