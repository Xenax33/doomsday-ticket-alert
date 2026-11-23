# Registers a Windows scheduled task that runs watch.py every 2 minutes while you're logged in.
# Remove it later with:  Unregister-ScheduledTask -TaskName DoomsdayTicketWatch -Confirm:$false
param([int]$EveryMinutes = 2)

$dir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$action  = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\watch.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
           -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "DoomsdayTicketWatch" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Checks Lahore cinemas for Avengers: Doomsday tickets" -Force | Out-Null
Write-Host "Registered DoomsdayTicketWatch: every $EveryMinutes min. Log: $dir\watch.log"
