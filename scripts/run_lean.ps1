# run_lean.ps1 —— 在「不打扰用户用电脑」的前提下跑一个耗资源的 python 脚本。
#
# 为什么需要它：这台机器 15.4 GB 物理内存 / 8 GB 显存，而且**是被用户正在用的**。
# 训练动辄十几小时，默认参数会开满 20 个线程、按正常优先级抢时间，结果就是用户自己的
# 编辑器卡住。这里做四件事：
#
#   1. **进程优先级降到 BelowNormal** —— 用户的一切前台操作都排在训练前面。
#      这是我实测之后仍然保留的一条：它几乎不损失吞吐（机器闲着时照样满速），
#      但用户一动手，训练就自动让路。
#   2. **限制数学库线程数**（默认 8/20 核）—— 不限制的话 torch/OpenMP/MKL 会按逻辑核数
#      开线程，把内存带宽吃干净。注意别压太低：实测压到 4 时单轮反而更慢，因为
#      mosaic 增广（每样本拼 4 张图）本来就是 CPU 密集的。
#   3. **显存分配策略** —— `expandable_segments` 减少碎片导致的假性 OOM。
#   4. **outputs 全部落盘** —— 训练十几小时，断了要能回看。
#
# ## 为什么收「一条命令串」而不是参数数组
#
# `-File` 调用时 PowerShell 把后面的东西按位置绑定，`-PyArgs a b c` 只会把 `a` 塞进数组，
# 后面几个的去向取决于绑定顺序 —— 想把同一条命令交给 `Start-Process` 脱离会话去跑时，
# 这种不确定性会直接变成「参数丢了」。所以这里收一个字符串、自己按空白切分。
# **路径里不能有空格**（本项目的权重名与脚本路径都没有）。
#
# ## 用法
#
#     # 前台（会等它跑完）
#     powershell -File scripts\run_lean.ps1 -Log outputs\logs\x.log -PyCommand "scripts\34_....py --tag t1"
#
#     # 脱离会话：关掉终端也继续跑
#     powershell -File scripts\run_lean.ps1 -Log outputs\logs\x.log -Detach -PyCommand "..."
#
#     # 看进度 / 停
#     Get-Content outputs\logs\x.log -Tail 20 -Encoding UTF8
#     Stop-Process -Id (Get-Content outputs\logs\x.pid)

param(
    [Parameter(Mandatory = $true)][string]$Log,
    [Parameter(Mandatory = $true)][string]$PyCommand,
    [int]$Threads = 8,
    [string]$Python = "C:\Users\Jack Tom\.conda\envs\az\python.exe",
    [switch]$Detach
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logPath = Join-Path $root $Log
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

if ($Detach) {
    # 用一条命令行把「同一个脚本、去掉 -Detach」再起一次，窗口隐藏、互不隶属。
    # 这样即使发起它的那个终端被关掉，训练也继续跑。
    $relaunch = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
                "-Log `"$Log`" -Threads $Threads -Python `"$Python`" " +
                "-PyCommand `"$PyCommand`""
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $relaunch -WindowStyle Hidden -PassThru
    $pidFile = [IO.Path]::ChangeExtension($logPath, ".pid")
    Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii
    "已脱离会话启动，pid $($proc.Id)（pid 已写入 $pidFile）"
    return
}

$env:OMP_NUM_THREADS = "$Threads"
$env:MKL_NUM_THREADS = "$Threads"
$env:OPENBLAS_NUM_THREADS = "$Threads"
$env:NUMEXPR_NUM_THREADS = "$Threads"
$env:PYTORCH_CUDA_ALLOC_CONF = "expandable_segments:True"

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
@(
    "===== $stamp 启动 ====="
    "python : $Python"
    "命令   : $PyCommand"
    "线程上限: $Threads（优先级 BelowNormal）"
    ""
) | Out-File -FilePath $logPath -Encoding utf8

# 按空白切分命令串（见文件头「为什么收一条命令串」）
$argv = @($PyCommand -split '\s+' | Where-Object { $_ })
$p = Start-Process -FilePath $Python `
    -ArgumentList (@("-X", "utf8") + $argv) `
    -NoNewWindow -PassThru -RedirectStandardOutput "$logPath.out" -RedirectStandardError "$logPath.err"

Set-Content -LiteralPath ([IO.Path]::ChangeExtension($logPath, ".pid")) -Value $p.Id -Encoding ascii

try {
    $p.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal
    "已设为 BelowNormal 优先级（pid $($p.Id)）" | Out-File -FilePath $logPath -Append -Encoding utf8
} catch {
    "设置优先级失败（不影响运行）：$($_.Exception.Message)" | Out-File -FilePath $logPath -Append -Encoding utf8
}

$p.WaitForExit()

# 合并 stdout / stderr 回主日志。
# **必须显式按 UTF-8 读、按 UTF-8 写**：子进程的 stdout 是 UTF-8，而 PowerShell 5.1 的
# Get-Content / Out-File 默认走系统 ANSI 代码页 —— 直接转发会把中文日志**二次编码**成乱码
# （实测「显存峰值」会变成「鏄惧瓨宄板€?」）。这个坑与 Set-Content 那个是同一个根源。
foreach ($suffix in @(".out", ".err")) {
    $f = "$logPath$suffix"
    if (Test-Path $f) {
        Get-Content $f -Encoding UTF8 | Out-File -FilePath $logPath -Append -Encoding utf8
        Remove-Item $f -Force
    }
}
"===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 退出码 $($p.ExitCode) =====" |
    Out-File -FilePath $logPath -Append -Encoding utf8
exit $p.ExitCode
