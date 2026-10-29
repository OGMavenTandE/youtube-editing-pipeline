# Run ytp take once for each new .mp4 or .mov after the file stops growing.
# Does not copy the take. Remembers files in .ytp-seen-takes.txt in the folder.
param(
    [Parameter(Mandatory = $true)][string]$Folder,
    [ValidateSet("talking-head", "ai-survival")][string]$Series = "talking-head",
    [int]$SettleSeconds = 3
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Folder)) {
    throw "Folder not found: $Folder"
}
$statePath = Join-Path $Folder ".ytp-seen-takes.txt"
$seen = @{}
if (Test-Path -LiteralPath $statePath) {
    Get-Content -LiteralPath $statePath | ForEach-Object {
        if ($_) { $seen[$_] = $true }
    }
}

Get-ChildItem -LiteralPath $Folder -File | Where-Object {
    $_.Extension -match '^\.(mp4|mov)$'
} | ForEach-Object {
    $full = $_.FullName
    if ($seen.ContainsKey($full)) {
        return
    }
    $size = (Get-Item -LiteralPath $full).Length
    Start-Sleep -Seconds $SettleSeconds
    if (-not (Test-Path -LiteralPath $full)) {
        return
    }
    $later = (Get-Item -LiteralPath $full).Length
    if ($later -ne $size) {
        Write-Host "still growing, skip: $full"
        return
    }
    Write-Host "ytp take $full"
    & ytp take "$full" --series $Series
    if ($LASTEXITCODE -ne 0) {
        throw "ytp take failed for $full"
    }
    Add-Content -LiteralPath $statePath -Value $full -Encoding utf8
}
