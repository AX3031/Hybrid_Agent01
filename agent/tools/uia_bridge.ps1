param(
    [Parameter(Mandatory = $true)][string]$InputFile,
    [Parameter(Mandatory = $true)][string]$OutputFile
)

# UI Automation + Windows.Media.Ocr bridge.
# Read:  {"action":"windows|tree|invoke|set_value|ocr", ...}
# Write: {"ok":true/false,"error":"...","data":{...}}
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function SafeInt([double]$v) {
    if ([double]::IsNaN($v) -or [double]::IsInfinity($v)) { return -1 }
    if ($v -gt 100000 -or $v -lt -100000) { return -1 }
    return [int][math]::Round($v)
}

function Rect-Of($el) {
    $r = $el.Current.BoundingRectangle
    return [pscustomobject]@{ x = (SafeInt $r.X); y = (SafeInt $r.Y); w = (SafeInt $r.Width); h = (SafeInt $r.Height) }
}

function Proc-Name([int]$processId) {
    try { return (Get-Process -Id $processId -ErrorAction Stop).ProcessName } catch { return '' }
}

# WinRT IAsyncOperation -> .NET Task, with the real exception surfaced.
$script:AsTaskGeneric = $null
function Await-Task($WinRtTask, $ResultType) {
    if (-not $script:AsTaskGeneric) {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime
        $script:AsTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
                $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
            })[0]
    }
    $asTask = $script:AsTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    try {
        $netTask.Wait(-1) | Out-Null
    } catch {
        if ($netTask.Exception) { throw $netTask.Exception.GetBaseException().Message }
        throw
    }
    if ($netTask.IsFaulted -and $netTask.Exception) {
        throw $netTask.Exception.GetBaseException().Message
    }
    return $netTask.Result
}

function Win-Info($el) {
    $rect = Rect-Of $el
    return [pscustomobject]@{
        title = $el.Current.Name
        cls   = $el.Current.ClassName
        pid   = $el.Current.ProcessId
        proc  = (Proc-Name $el.Current.ProcessId)
        x = $rect.x; y = $rect.y; w = $rect.w; h = $rect.h
        minimized = [bool]($el.Current.IsOffscreen)
    }
}

function All-Windows() {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Window)
    return $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
}

function Find-Window($spec) {
    $pidWanted = 0
    if ($spec.pid) { $pidWanted = [int]$spec.pid }
    $title = ''
    if ($spec.window) { $title = [string]$spec.window }

    # No pid and no title: operate on the focused window (what the user is looking at).
    if ($pidWanted -le 0 -and -not $title) {
        $fg = [System.Windows.Automation.AutomationElement]::FocusedElement
        if (-not $fg) { return $null }
        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $cur = $fg
        $depth = 0
        while ($cur -and $depth -lt 12) {
            $parent = $walker.GetParent($cur)
            if (-not $parent) { break }
            $parentType = Type-Name $parent
            if ($parentType -eq 'Pane' -and $parent.Current.ClassName -eq '#32769') { break }
            if (-not $parent.Current.Name -and $parentType -eq 'Pane') { break }
            $cur = $parent
            $depth++
        }
        return $cur
    }

    $best = $null
    foreach ($w in (All-Windows)) {
        $info = Win-Info $w
        if ($pidWanted -gt 0 -and $info.pid -ne $pidWanted) { continue }
        if ($title) {
            if ($info.title -notlike "*$title*") { continue }
        }
        if ($info.w -le 0 -or $info.h -le 0) { continue }
        if ($info.minimized -and $best) { continue }
        $best = $w
        if (-not $info.minimized -and -not $title) { break }
    }
    return $best
}

function Type-Name($el) {
    $ct = $el.Current.ControlType
    if (-not $ct) { return '' }
    return ($ct.ProgrammaticName -replace '^ControlType\.', '')
}

function Node-Info($el, [int]$index) {
    $rect = Rect-Of $el
    $password = $false
    try {
        $password = [bool]$el.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::IsPasswordProperty)
    } catch { $password = $false }
    $value = ''
    if (-not $password) {
        try {
            $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $value = [string]$vp.Current.Value
        } catch { $value = '' }
    }
    return [pscustomobject]@{
        i = $index
        name = $el.Current.Name
        type = (Type-Name $el)
        aid = $el.Current.AutomationId
        cls = $el.Current.ClassName
        x = $rect.x; y = $rect.y; w = $rect.w; h = $rect.h
        enabled = [bool]$el.Current.IsEnabled
        offscreen = [bool]$el.Current.IsOffscreen
        password = $password
        value = $value
    }
}

function Find-Element($win, $spec) {
    # 1) index into a previous snapshot is not resolvable here; caller re-sends name/aid
    $name = ''
    if ($spec.name) { $name = [string]$spec.name }
    $aid = ''
    if ($spec.aid) { $aid = [string]$spec.aid }
    $typeWanted = ''
    if ($spec.type) { $typeWanted = [string]$spec.type }
    if (-not $name -and -not $aid) { throw 'need name or aid' }

    $conditions = @()
    if ($name) {
        $conditions += (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, $name))
    }
    if ($aid) {
        $conditions += (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid))
    }
    $cond = $conditions[0]
    for ($i = 1; $i -lt $conditions.Count; $i++) {
        $cond = New-Object System.Windows.Automation.AndCondition($cond, $conditions[$i])
    }

    $found = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    foreach ($el in $found) {
        $info = Node-Info $el 0
        if ($info.offscreen) { continue }
        if ($typeWanted -and ($info.type -ne $typeWanted)) { continue }
        return $el
    }

    # Fallback: substring match on Name (more forgiving for menu items)
    if ($name) {
        $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition)
        $limit = 5000
        foreach ($el in $all) {
            $limit--
            if ($limit -le 0) { break }
            $text = $el.Current.Name
            if (-not $text) { continue }
            if ($text.IndexOf($name, [StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            $info = Node-Info $el 0
            if ($info.offscreen) { continue }
            if ($typeWanted -and ($info.type -ne $typeWanted)) { continue }
            return $el
        }
    }
    return $null
}

function Do-Windows($spec) {
    $rows = @()
    $limit = 60
    if ($spec.limit) { $limit = [int]$spec.limit }
    $filter = ''
    if ($spec.filter) { $filter = [string]$spec.filter }

    $fg = [System.Windows.Automation.AutomationElement]::FocusedElement
    $fgPid = 0
    if ($fg) { try { $fgPid = $fg.Current.ProcessId } catch { $fgPid = 0 } }

    foreach ($w in (All-Windows)) {
        $info = Win-Info $w
        if ($info.w -le 0 -or $info.h -le 0) { continue }
        if ($filter -and ($info.title -notlike "*$filter*")) { continue }
        $info | Add-Member -NotePropertyName foreground -NotePropertyValue ($info.pid -eq $fgPid)
        $rows += $info
        if ($rows.Count -ge $limit) { break }
    }
    return [pscustomobject]@{ windows = $rows }
}

function Do-Window($spec) {
    $win = Find-Window $spec
    if (-not $win) { throw 'window not found' }
    return [pscustomobject]@{ window = (Win-Info $win) }
}

function Do-Tree($spec) {
    $win = Find-Window $spec
    if (-not $win) { throw 'window not found' }
    $depthLimit = 4
    if ($spec.depth) { $depthLimit = [int]$spec.depth }
    $nodeLimit = 200
    if ($spec.limit) { $nodeLimit = [int]$spec.limit }
    $filter = ''
    if ($spec.filter) { $filter = [string]$spec.filter }

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $nodes = New-Object System.Collections.ArrayList
    $stack = New-Object System.Collections.Stack
    $stack.Push(@($walker.GetFirstChild($win), 1))
    while ($stack.Count -gt 0 -and $nodes.Count -lt $nodeLimit) {
        $item = $stack.Pop()
        $el = $item[0]
        $depth = [int]$item[1]
        if (-not $el) { continue }
        $info = Node-Info $el ($nodes.Count)
        if ($filter -and ($info.name -notlike "*$filter*") -and ($info.type -ne 'Edit') -and ($info.type -ne 'Button')) {
            # keep walking even when filtered out
        } else {
            [void]$nodes.Add($info)
        }
        if ($depth -lt $depthLimit) {
            $child = $walker.GetFirstChild($el)
            while ($child) {
                $stack.Push(@($child, ($depth + 1)))
                $child = $walker.GetNextSibling($child)
            }
        }
    }
    return [pscustomobject]@{ window = (Win-Info $win); nodes = $nodes }
}

function Do-Invoke($spec) {
    $win = Find-Window $spec
    if (-not $win) { throw 'window not found' }
    $el = Find-Element $win $spec
    if (-not $el) { throw 'element not found' }

    $info = Node-Info $el 0
    if ($info.password) { throw 'element is a password field' }

    try {
        $pattern = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke()
        return [pscustomobject]@{ how = 'invoke'; window = (Win-Info $win); element = $info }
    } catch {
        # fall through to coordinate click
    }
    try {
        $point = $el.GetClickablePoint()
        return [pscustomobject]@{ how = 'point'; x = (SafeInt $point.X); y = (SafeInt $point.Y); window = (Win-Info $win); element = $info }
    } catch {
        throw 'element has no invoke pattern and no clickable point'
    }
}

function Do-SetValue($spec) {
    $win = Find-Window $spec
    if (-not $win) { throw 'window not found' }
    $el = Find-Element $win $spec
    if (-not $el) { throw 'element not found' }
    $info = Node-Info $el 0
    if ($info.password) { throw 'refuse to write into a password field' }
    $text = ''
    if ($null -ne $spec.text) { $text = [string]$spec.text }

    try {
        $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($vp.Current.IsReadOnly) { throw 'element is read-only' }
        $vp.SetValue($text)
        return [pscustomobject]@{ how = 'value'; window = (Win-Info $win); element = $info }
    } catch {
        $msg = $_.Exception.Message
    }
    try {
        $point = $el.GetClickablePoint()
        return [pscustomobject]@{ how = 'point'; x = (SafeInt $point.X); y = (SafeInt $point.Y); window = (Win-Info $win); element = $info; note = $msg }
    } catch {
        throw ('no ValuePattern and no clickable point: ' + $msg)
    }
}

function Do-Ocr($spec) {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]

    $path = [string]$spec.image
    if (-not (Test-Path $path)) { throw "image not found: $path" }

    $file = Await-Task ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await-Task ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await-Task ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Task ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    # RecognizeAsync only accepts Bgra8/Premultiplied in practice.
    $converted = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert(
        $bitmap,
        [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $engine) {
        $langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
        foreach ($l in $langs) {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($l)
            if ($engine) { break }
        }
    }
    if (-not $engine) { throw 'no OCR language pack installed' }

    $res = Await-Task ($engine.RecognizeAsync($converted)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($line in @($res.Lines)) {
        $words = @($line.Words)
        if ($words.Count -eq 0) { continue }
        $minX = 1e9; $minY = 1e9; $maxX = -1e9; $maxY = -1e9
        foreach ($w in $words) {
            $r = $w.BoundingRect
            $x1 = $r.X; $y1 = $r.Y; $x2 = $r.X + $r.Width; $y2 = $r.Y + $r.Height
            if ($x1 -lt $minX) { $minX = $x1 }
            if ($y1 -lt $minY) { $minY = $y1 }
            if ($x2 -gt $maxX) { $maxX = $x2 }
            if ($y2 -gt $maxY) { $maxY = $y2 }
        }
        $lines += [pscustomobject]@{
            text = $line.Text
            x = (SafeInt $minX); y = (SafeInt $minY)
            w = (SafeInt ($maxX - $minX)); h = (SafeInt ($maxY - $minY))
        }
    }
    return [pscustomobject]@{
        lang = $engine.RecognizerLanguage.LanguageTag
        image_w = [int]$decoder.PixelWidth
        image_h = [int]$decoder.PixelHeight
        lines = $lines
    }
}

$payload = Get-Content -Raw -Encoding UTF8 -Path $InputFile | ConvertFrom-Json
$action = [string]$payload.action

$result = $null
try {
    switch ($action) {
        'windows' { $result = Do-Windows $payload }
        'window' { $result = Do-Window $payload }
        'tree' { $result = Do-Tree $payload }
        'invoke' { $result = Do-Invoke $payload }
        'set_value' { $result = Do-SetValue $payload }
        'ocr' { $result = Do-Ocr $payload }
        default { throw "unknown bridge action: $action" }
    }
    $out = [pscustomobject]@{ ok = $true; error = ''; data = $result }
} catch {
    $out = [pscustomobject]@{ ok = $false; error = $_.Exception.Message; data = $null }
}

$json = ConvertTo-Json -InputObject $out -Depth 6 -Compress
[System.IO.File]::WriteAllText($OutputFile, $json, (New-Object System.Text.UTF8Encoding($false)))
