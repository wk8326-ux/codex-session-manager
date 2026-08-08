[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace LocalProjectConsole {
    public static class Dpi {
        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetProcessDPIAware();
    }
}
"@

[LocalProjectConsole.Dpi]::SetProcessDPIAware() | Out-Null
$virtualScreen = [System.Windows.Forms.SystemInformation]::VirtualScreen
if ($virtualScreen.Width -lt 1 -or $virtualScreen.Height -lt 1) {
    exit 1
}

$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
[System.IO.Directory]::CreateDirectory(
    [System.IO.Path]::GetDirectoryName($resolvedOutput)
) | Out-Null
if (Test-Path -LiteralPath $resolvedOutput) {
    Remove-Item -LiteralPath $resolvedOutput -Force
}

$desktop = New-Object System.Drawing.Bitmap(
    $virtualScreen.Width,
    $virtualScreen.Height,
    [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
)
$desktopGraphics = [System.Drawing.Graphics]::FromImage($desktop)
try {
    $desktopGraphics.CopyFromScreen(
        $virtualScreen.Left,
        $virtualScreen.Top,
        0,
        0,
        $virtualScreen.Size,
        [System.Drawing.CopyPixelOperation]::SourceCopy
    )
}
finally {
    $desktopGraphics.Dispose()
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.SetBounds(
    $virtualScreen.Left,
    $virtualScreen.Top,
    $virtualScreen.Width,
    $virtualScreen.Height
)
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.KeyPreview = $true
$form.Cursor = [System.Windows.Forms.Cursors]::Cross
$form.BackgroundImage = $desktop
$form.BackgroundImageLayout = [System.Windows.Forms.ImageLayout]::None
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::None

$script:dragging = $false
$script:captured = $false
$script:startPoint = [System.Drawing.Point]::Empty
$script:selection = [System.Drawing.Rectangle]::Empty

function Get-SelectionRectangle {
    param(
        [System.Drawing.Point]$Start,
        [System.Drawing.Point]$End
    )

    $left = [Math]::Min($Start.X, $End.X)
    $top = [Math]::Min($Start.Y, $End.Y)
    $right = [Math]::Max($Start.X, $End.X)
    $bottom = [Math]::Max($Start.Y, $End.Y)
    return [System.Drawing.Rectangle]::FromLTRB($left, $top, $right, $bottom)
}

$form.Add_Paint({
    param($sender, $eventArgs)

    $shade = New-Object System.Drawing.SolidBrush(
        [System.Drawing.Color]::FromArgb(112, 0, 0, 0)
    )
    try {
        $eventArgs.Graphics.FillRectangle($shade, $form.ClientRectangle)
        if ($script:selection.Width -gt 0 -and $script:selection.Height -gt 0) {
            $eventArgs.Graphics.DrawImage(
                $desktop,
                $script:selection,
                $script:selection,
                [System.Drawing.GraphicsUnit]::Pixel
            )
            $border = New-Object System.Drawing.Pen(
                [System.Drawing.Color]::FromArgb(255, 255, 255, 255),
                1
            )
            try {
                $eventArgs.Graphics.DrawRectangle($border, $script:selection)
            }
            finally {
                $border.Dispose()
            }
        }
    }
    finally {
        $shade.Dispose()
    }
})

$form.Add_MouseDown({
    param($sender, $eventArgs)

    if ($eventArgs.Button -eq [System.Windows.Forms.MouseButtons]::Right) {
        $form.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
        $form.Close()
        return
    }
    if ($eventArgs.Button -ne [System.Windows.Forms.MouseButtons]::Left) {
        return
    }
    $script:dragging = $true
    $script:startPoint = $eventArgs.Location
    $script:selection = [System.Drawing.Rectangle]::Empty
    $form.Capture = $true
    $form.Invalidate()
})

$form.Add_MouseMove({
    param($sender, $eventArgs)

    if (-not $script:dragging) {
        return
    }
    $script:selection = Get-SelectionRectangle $script:startPoint $eventArgs.Location
    $form.Invalidate()
})

$form.Add_MouseUp({
    param($sender, $eventArgs)

    if (
        -not $script:dragging -or
        $eventArgs.Button -ne [System.Windows.Forms.MouseButtons]::Left
    ) {
        return
    }
    $script:dragging = $false
    $form.Capture = $false
    $script:selection = Get-SelectionRectangle $script:startPoint $eventArgs.Location
    if ($script:selection.Width -lt 2 -or $script:selection.Height -lt 2) {
        $script:selection = [System.Drawing.Rectangle]::Empty
        $form.Invalidate()
        return
    }

    $cropped = $desktop.Clone(
        $script:selection,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    )
    try {
        $cropped.Save(
            $resolvedOutput,
            [System.Drawing.Imaging.ImageFormat]::Png
        )
    }
    finally {
        $cropped.Dispose()
    }
    $script:captured = $true
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})

$form.Add_KeyDown({
    param($sender, $eventArgs)

    if ($eventArgs.KeyCode -eq [System.Windows.Forms.Keys]::Escape) {
        $form.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
        $form.Close()
    }
})

try {
    $form.Activate()
    $form.ShowDialog() | Out-Null
}
finally {
    $form.Dispose()
    $desktop.Dispose()
}

if ($script:captured -and (Test-Path -LiteralPath $resolvedOutput)) {
    exit 0
}
if (Test-Path -LiteralPath $resolvedOutput) {
    Remove-Item -LiteralPath $resolvedOutput -Force
}
exit 2
