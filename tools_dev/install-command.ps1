# ===================================================================
#  把 xztcode 注册成全局命令（每个用户只需要跑一次）
#
#  做的事情：
#    1. 在 %USERPROFILE%\.xztcode\ 下写三份项目路径记录
#    2. 把启动脚本放进 %USERPROFILE%\.xztcode\bin\xztcode.bat
#    3. 把 bin 目录加进「用户级」PATH（不需要管理员权限）
#
#  之后在任意目录的 cmd / PowerShell 里敲 xztcode 都能启动。
#
#  用法（在项目文件夹里开 PowerShell 或 CMD）：
#      powershell -ExecutionPolicy Bypass -File tools_dev\install-command.ps1
#
#  注意：修改用户级 PATH 需要写注册表，普通权限即可，但必须在
#  你自己的终端里执行 —— 受限沙箱里这一步会被系统拒绝。
# ===================================================================

$ErrorActionPreference = 'Stop'

# ---- 定位项目根目录（本脚本在 tools_dev\ 下）--------------------------
$projectRoot = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path (Join-Path $projectRoot 'main.py'))) {
    Write-Host "[错误] 在 $projectRoot 下找不到 main.py，请确认脚本位置。" -ForegroundColor Red
    exit 1
}

$source = Join-Path $projectRoot 'xztcode.bat'
if (-not (Test-Path $source)) {
    Write-Host "[错误] 找不到 $source" -ForegroundColor Red
    exit 1
}

# ---- 目标目录 --------------------------------------------------------
$storeDir = Join-Path $env:USERPROFILE '.xztcode'
$binDir   = Join-Path $storeDir 'bin'

New-Item -ItemType Directory -Force -Path $binDir | Out-Null

# ---- 记录项目路径 ----------------------------------------------------
# 项目路径里通常带中文（比如「桌面」「软件项目管理」），而 xztcode.bat 是用
# cmd 跑的：cmd 解码文本文件用的是**当前活动代码页**，写入方和读取方编码
# 一旦不一致，路径就会变成乱码。
#
# 所以这里写两份，xztcode.bat 也按固定顺序试：
#
#   project-utf8.txt  UTF-8 编码。脚本先 chcp 65001 再读，读写两侧都是
#                     UTF-8，完全自洽 —— 这是主用的一份。
#   project.txt       按系统默认代码页写。万一 65001 在某些老环境里不可用，
#                     还有这一份顶着。
#
# 实测：只要 UTF-8 那份在，无论控制台原本是什么代码页都能正确启动。

# 1) 主用：UTF-8（不带 BOM）
[System.IO.File]::WriteAllText(
    (Join-Path $storeDir 'project-utf8.txt'),
    $projectRoot,
    (New-Object System.Text.UTF8Encoding($false))
)

# 2) 备用：系统默认代码页（中文 Windows 上就是 GBK）
[System.IO.File]::WriteAllText(
    (Join-Path $storeDir 'project.txt'),
    $projectRoot,
    [System.Text.Encoding]::Default
)

# ---- 放一份启动脚本 --------------------------------------------------
Copy-Item -Path $source -Destination (Join-Path $binDir 'xztcode.bat') -Force

Write-Host ""
Write-Host "项目目录 : $projectRoot"
Write-Host "安装位置 : $binDir"

# ---- 加进用户级 PATH -------------------------------------------------
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }

$cleanPath = $userPath.TrimEnd(';')
$already = $false
foreach ($part in $cleanPath.Split(';')) {
    if ($part.Trim().TrimEnd('\') -ieq $binDir.TrimEnd('\')) { $already = $true }
}

if ($already) {
    Write-Host "PATH 里已经有这个目录，跳过。" -ForegroundColor Yellow
} else {
    if ($cleanPath -eq '') {
        $newPath = $binDir
    } else {
        $newPath = "$cleanPath;$binDir"
    }

    try {
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Host "已把 $binDir 加入用户级 PATH。" -ForegroundColor Green
    } catch {
        Write-Host ""
        Write-Host "[错误] 无法写入用户级 PATH：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host "可以手动添加这个目录到「环境变量 → 用户变量 → Path」："
        Write-Host "    $binDir"
        exit 1
    }
}

# ---- 提示 ------------------------------------------------------------
Write-Host ""
Write-Host "完成。接下来：" -ForegroundColor Cyan
Write-Host "  1. 关掉当前终端，重新开一个（PATH 变更只对新窗口生效）"
Write-Host "  2. 在任意目录敲：xztcode"
Write-Host ""
Write-Host "不想重开窗口的话，在当前窗口先执行："
Write-Host "  `$env:Path += ';$binDir'"
Write-Host ""
Write-Host "卸载：删掉 $storeDir，再从 PATH 里移除 $binDir。"
