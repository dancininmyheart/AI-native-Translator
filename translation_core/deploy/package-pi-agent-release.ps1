param(
    [string]$ReleaseTag = (Get-Date -Format "yyyyMMdd")
)

$ErrorActionPreference = "Stop"

if ($ReleaseTag -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
    throw "ReleaseTag must be 1-64 characters using only letters, digits, dot, underscore, or hyphen."
}

$coreRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = (Resolve-Path (Join-Path $coreRoot "..")).Path
$skillRoot = Join-Path $workspaceRoot "document-translation"
$releaseRoot = Join-Path $coreRoot "release"

if (-not (Test-Path -LiteralPath (Join-Path $coreRoot "pi_agent/package-lock.json"))) {
    throw "Pi Agent package-lock.json is missing."
}
if (-not (Test-Path -LiteralPath (Join-Path $skillRoot "SKILL.md"))) {
    throw "The document-translation Skill is missing."
}

New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
$archiveName = "format-translator-pi-agent-linux-$ReleaseTag.tar.gz"
$archivePath = Join-Path $releaseRoot $archiveName
$checksumPath = "$archivePath.sha256"
$lockIdentity = [System.IO.Path]::GetFullPath($archivePath).ToUpperInvariant()
$lockHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $lockHashBytes = $lockHasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($lockIdentity))
}
finally {
    $lockHasher.Dispose()
}
$lockHash = [System.BitConverter]::ToString($lockHashBytes).Replace("-", "").ToLowerInvariant()
$releaseMutex = [System.Threading.Mutex]::new($false, "format-translator-pi-agent-$lockHash")
$lockAcquired = $false
$temporaryArchivePath = $null
$temporaryChecksumPath = $null
$promotedArchive = $false
$promotedChecksum = $false

try {
    try {
        $lockAcquired = $releaseMutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        # The previous owner exited without releasing the target lock. The
        # current process owns the abandoned mutex and can safely continue.
        $lockAcquired = $true
    }
    if (-not $lockAcquired) {
        throw "Release output is currently being created: $archivePath"
    }

    if ((Test-Path -LiteralPath $archivePath) -or (Test-Path -LiteralPath $checksumPath)) {
        throw "Release output already exists: $archivePath"
    }

    $temporarySuffix = "$PID.$([guid]::NewGuid().ToString('N'))"
    $temporaryArchivePath = Join-Path $releaseRoot ".$archiveName.$temporarySuffix.tmp"
    $temporaryChecksumPath = "$temporaryArchivePath.sha256"

    $indexTreeOutput = @(& git -C $workspaceRoot write-tree)
    if ($LASTEXITCODE -ne 0) {
        throw "git write-tree failed with exit code $LASTEXITCODE"
    }
    if ($indexTreeOutput.Count -ne 1 -or $indexTreeOutput[0] -notmatch '^[0-9a-f]{40,64}$') {
        throw "git write-tree returned an invalid tree object id."
    }
    $indexTree = $indexTreeOutput[0]

    # Archive the explicit release roots from the Git index. A fixed timestamp
    # makes the gzip and tar bytes reproducible for an identical staged tree.
    & git -C $workspaceRoot archive `
        --format=tar.gz `
        --mtime=1970-01-01T00:00:00Z `
        "--output=$temporaryArchivePath" `
        $indexTree `
        -- `
        translation_core `
        document-translation
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed with exit code $LASTEXITCODE"
    }

    $entries = @(& tar -tzf $temporaryArchivePath)
    if ($LASTEXITCODE -ne 0 -or $entries.Count -eq 0) {
        throw "The generated archive cannot be listed."
    }
    $forbidden = $entries | Where-Object {
        (($_ -match '(^|/)\.env($|\.)') -and ($_ -notmatch '\.example$')) -or
        ($_ -match '(^|/)(\.npmrc|\.pypirc|credentials\.json|service-account[^/]*\.json)$') -or
        ($_ -match '(^|/)(id_rsa[^/]*|id_ed25519[^/]*)$') -or
        ($_ -match '(^|/)(node_modules|dist|\.venv|\.validation|\.omo|\.pytest[^/]*|\.ruff_cache|\.codegraph|\.git|release|__pycache__)(/|$)') -or
        ($_ -match '\.(pyc|pyo|pem|key|p12|pfx|jks|keystore|kdbx|log|doc|docx|ppt|pptx|pdf|zip|7z|rar|tgz|gz)$')
    }
    if ($forbidden) {
        throw "Forbidden release entries detected: $($forbidden -join ', ')"
    }

    $requiredEntries = @(
        "translation_core/pyproject.toml",
        "translation_core/pi_agent/package-lock.json",
        "translation_core/deploy/linux/Dockerfile.agent",
        "translation_core/deploy/linux/docker-compose.pi.yml",
        "document-translation/SKILL.md"
    )
    foreach ($required in $requiredEntries) {
        if ($entries -notcontains $required) {
            throw "Release entry is missing: $required"
        }
    }

    $digest = (Get-FileHash -LiteralPath $temporaryArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $temporaryChecksumPath,
        "$digest  $archiveName`n",
        $utf8NoBom
    )

    # File.Move is a non-overwriting rename on the same volume. Promote only
    # after every archive validation and checksum operation has succeeded.
    [System.IO.File]::Move($temporaryArchivePath, $archivePath)
    $promotedArchive = $true
    [System.IO.File]::Move($temporaryChecksumPath, $checksumPath)
    $promotedChecksum = $true

    [pscustomobject]@{
        archive = $archivePath
        checksum = $checksumPath
        sha256 = $digest
        entry_count = $entries.Count
        size_bytes = (Get-Item -LiteralPath $archivePath).Length
    } | Format-List
}
catch {
    # A failed promotion must not expose a partial release pair. Existing
    # outputs are never removed because the flags are set only after our moves.
    if ($promotedChecksum -and (Test-Path -LiteralPath $checksumPath)) {
        Remove-Item -LiteralPath $checksumPath -Force -ErrorAction SilentlyContinue
    }
    if ($promotedArchive -and (Test-Path -LiteralPath $archivePath)) {
        Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    foreach ($temporaryPath in @($temporaryArchivePath, $temporaryChecksumPath)) {
        if ($temporaryPath -and (Test-Path -LiteralPath $temporaryPath)) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
    if ($lockAcquired) {
        $releaseMutex.ReleaseMutex()
    }
    $releaseMutex.Dispose()
}
