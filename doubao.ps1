param([string]$Images="", [string]$Text="", [string]$Wait="18", [string]$Exe="", [string]$Out="", [string]$Shot="")

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$sig = @"
using System;
using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int W, int H, bool repaint);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, int dwExtraInfo);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
Add-Type -TypeDefinition $sig
try { [W32]::SetProcessDPIAware() | Out-Null } catch {}

function Write-Result($obj) {
  $json = $obj | ConvertTo-Json -Compress -Depth 5
  [System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
}
function Fail($msg) { Write-Result @{ok=$false; error=$msg}; exit 0 }


function Get-BigDoubaoWindow() {
  # 豆包有很多辅助进程（MainWindowHandle=0）。主界面那个才有标题。
  # 注意：窗口最小化时 GetWindowRect 只返回 ~160x28 的标题条区域，
  #       所以这里不能按尺寸过滤，只按「有标题 + 面积最大」挑，挑完再还原。
  $cands = @(Get-Process -Name Doubao -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
  if ($cands.Count -eq 0) { return [IntPtr]::Zero }
  $best = [IntPtr]::Zero; $bestArea = -1; $bestTitled = $false
  foreach ($cp in $cands) {
    $hw = $cp.MainWindowHandle
    $rr = New-Object W32+RECT
    [W32]::GetWindowRect($hw, [ref]$rr) | Out-Null
    $w = [int]($rr.Right - $rr.Left); $hh = [int]($rr.Bottom - $rr.Top)
    $titled = -not [string]::IsNullOrWhiteSpace($cp.MainWindowTitle)
    $area = ($w * $hh) + ($(if ($titled) { 100000000 } else { 0 }))
    if ($area -gt $bestArea) { $bestArea = $area; $best = $hw; $bestTitled = $titled }
  }
  return $best
}

function Ensure-Window($h) {
  # 还原 + 放大到能看清回答的尺寸，返回最终矩形
  if ([W32]::IsIconic($h)) {
    [W32]::ShowWindow($h, 9) | Out-Null
    Start-Sleep -Milliseconds 900
  } else {
    [W32]::ShowWindow($h, 5) | Out-Null
    Start-Sleep -Milliseconds 300
  }
  $rr = New-Object W32+RECT
  [W32]::GetWindowRect($h, [ref]$rr) | Out-Null
  $w = [int]($rr.Right - $rr.Left); $hh = [int]($rr.Bottom - $rr.Top)
  if ($w -lt 900 -or $hh -lt 700) {
    $sw = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width
    $sh = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height
    $nw = [int]([Math]::Min(1180, $sw - 80)); $nh = [int]([Math]::Min(900, $sh - 80))
    [W32]::MoveWindow($h, [int](($sw - $nw) / 2), [int](($sh - $nh) / 2), $nw, $nh, $true) | Out-Null
    Start-Sleep -Milliseconds 900
  }
  return $h
}

function Focus($h) {
  for ($i = 0; $i -lt 10; $i++) {
    [W32]::keybd_event(0x12, 0, 0, 0)
    [W32]::keybd_event(0x12, 0, 2, 0)
    [W32]::BringWindowToTop($h) | Out-Null
    [W32]::SetForegroundWindow($h) | Out-Null
    Start-Sleep -Milliseconds 250
    if ([W32]::GetForegroundWindow() -eq $h) { return $true }
  }
  return $false
}

try {
  $p = Get-Process -Name Doubao -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if (-not $p) {
    if (-not (Test-Path $Exe)) { Fail "找不到豆包程序：$Exe" }
    Start-Process -FilePath $Exe -WorkingDirectory (Split-Path $Exe) | Out-Null
    for ($i = 0; $i -lt 40; $i++) {
      Start-Sleep -Milliseconds 500
      $p = Get-Process -Name Doubao -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
      if ($p) { break }
    }
  }
  if (-not $p) { Fail "豆包窗口没出现（可能还在启动，等几秒再点一次）" }

  $h = Get-BigDoubaoWindow
  if ($h -eq [IntPtr]::Zero) { $h = $p.MainWindowHandle }
  $h = Ensure-Window $h


  if (-not (Focus $h)) { Fail "没能把豆包窗口切到前台（被别的窗口挡住了）。先把豆包点开放在最前面，再点一次。" }

  $r = New-Object W32+RECT
  [W32]::GetWindowRect($h, [ref]$r) | Out-Null
  $cx = [int](($r.Left + $r.Right) / 2)
  $cy = [int]($r.Bottom - 110)
  [W32]::SetCursorPos($cx, $cy) | Out-Null
  Start-Sleep -Milliseconds 250
  [W32]::mouse_event(0x02, 0, 0, 0, 0)
  [W32]::mouse_event(0x04, 0, 0, 0, 0)
  Start-Sleep -Milliseconds 800
  if ([W32]::GetForegroundWindow() -ne $h) { Fail "点击后焦点跑了（没停在豆包上），为安全起见没有粘贴。请先手动点开豆包再试。" }

  # 先清空输入框，避免把上一次没发出去的内容带进来
  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 200
  [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
  Start-Sleep -Milliseconds 350

  # 先把图片逐张粘进输入框
  if ($Images) {
    foreach ($f in $Images.Split("|")) {
      if ($f -and (Test-Path $f)) {
        $img = [System.Drawing.Image]::FromFile($f)
        [System.Windows.Forms.Clipboard]::SetImage($img)
        $img.Dispose()
        Start-Sleep -Milliseconds 450
        [System.Windows.Forms.SendKeys]::SendWait("^v")
        Start-Sleep -Milliseconds 1100
        if ([W32]::GetForegroundWindow() -ne $h) { Fail "粘贴图片时焦点被抢走了，已停止。请让豆包保持在前台再试。" }
      }
    }
  }

  # 再把文字要求粘进去（中文只能用剪贴板，SendKeys 打不出中文）
  if ($Text) {
    [System.Windows.Forms.Clipboard]::SetText($Text)
    Start-Sleep -Milliseconds 350
    [System.Windows.Forms.SendKeys]::SendWait("^v")
    Start-Sleep -Milliseconds 700
  }

  [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  Start-Sleep -Seconds ([int]$Wait)

  if ([W32]::GetForegroundWindow() -ne $h) {
    if (-not (Focus $h)) { Fail "等回答时豆包被切到后台了，为安全起见没有截屏。请让豆包保持在前台再试。" }
    Start-Sleep -Milliseconds 600
  }

  if (-not $Shot) { Fail "内部错误：没有给截图路径" }

  function Get-Rect($hw) {
    $rr = New-Object W32+RECT
    [W32]::GetWindowRect($hw, [ref]$rr) | Out-Null
    return @{ L = [int]$rr.Left; T = [int]$rr.Top; W = [int]($rr.Right - $rr.Left); H = [int]($rr.Bottom - $rr.Top) }
  }

  # 豆包可能重建窗口 / 有多个进程，重新挑一次最大的可见窗口
  $big = Get-BigDoubaoWindow
  if ($big -ne [IntPtr]::Zero) { $h = $big }
  $h = Ensure-Window $h

  $rc = Get-Rect $h
  if ($rc.W -lt 900 -or $rc.H -lt 700) {
    $sw = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width
    $sh = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height
    $nw = [int]([Math]::Min(1180, $sw - 80)); $nh = [int]([Math]::Min(900, $sh - 80))
    [W32]::MoveWindow($h, [int](($sw - $nw) / 2), [int](($sh - $nh) / 2), $nw, $nh, $true) | Out-Null
    Start-Sleep -Milliseconds 900
    $rc = Get-Rect $h
  }
  if ($rc.W -lt 200 -or $rc.H -lt 200) {
    Fail ("豆包窗口太小，没法截取回答区域（当前 " + $rc.W + "x" + $rc.H + "），请把豆包窗口拖大一点再试")
  }
  $r = New-Object W32+RECT
  $r.Left = $rc.L; $r.Top = $rc.T; $r.Right = ($rc.L + $rc.W); $r.Bottom = ($rc.T + $rc.H)
  $wd = $rc.W; $ht = $rc.H

  $bmp = New-Object System.Drawing.Bitmap $wd, $ht
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size $wd, $ht))
  $g.Dispose()
  $bmp.Save($Shot, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()

  Write-Result @{ok=$true; shot=$Shot}
} catch {
  Fail ("自动化失败：" + $_.Exception.Message)
}
