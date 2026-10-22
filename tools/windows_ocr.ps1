param([Parameter(Mandatory=$true)][string]$InputPath,
      [Parameter(Mandatory=$true)][string]$OutputPath,
      [string]$LanguageTag='zh-Hans-CN')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($Operation, $ResultType) {
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    try { $task.Wait() } catch { throw ($ResultType.FullName + ': ' + $task.Exception.ToString()) }
    return $task.Result
}
$language = New-Object Windows.Globalization.Language($LanguageTag)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw 'Simplified Chinese Windows OCR is not installed' }
$stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer = New-Object Windows.Storage.Streams.DataWriter($stream)
try {
    if ($InputPath -eq '__BD2_PIPE__') {
        $inputBytes = [Convert]::FromBase64String([Console]::In.ReadToEnd())
    } else { $inputBytes = [IO.File]::ReadAllBytes($InputPath) }
    $writer.WriteBytes($inputBytes)
    $null = Await-Result ($writer.StoreAsync()) ([uint32])
    $null = $writer.DetachStream()
} finally {
    $writer.Dispose()
}
$stream.Seek(0)
$bitmap = $null
try {
    $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object {
        $line = $_
        $words = @($line.Words | ForEach-Object {
            @{ text=$_.Text; x=$_.BoundingRect.X; y=$_.BoundingRect.Y;
               width=$_.BoundingRect.Width; height=$_.BoundingRect.Height }
        })
        @{ text=$line.Text; words=$words }
    })
    $payload = @{ language=$engine.RecognizerLanguage.LanguageTag; width=$bitmap.PixelWidth;
                  height=$bitmap.PixelHeight; lines=$lines }
    $json = ConvertTo-Json -InputObject $payload -Depth 8
    if ($OutputPath -eq '__BD2_PIPE__') {
        [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
        [Console]::Out.Write($json)
    } else { [IO.File]::WriteAllText($OutputPath, $json, (New-Object Text.UTF8Encoding($false))) }
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    $stream.Dispose()
}
