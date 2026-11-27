# first-logon.ps1: configures the Windows template on its first logon, then generalizes it.
#
# provision-template.yml copies this script onto the seed ISO and the answer file runs it once,
# elevated, as the administrator (design D3 of add-windows-github-runner). It replaces the
# one-time password, installs the QEMU guest agent, enables OpenSSH with PowerShell as the default
# shell, installs the management public key, applies the baseline (no RDP, no SMB inbound, no
# WinRM listener), removes the one-time password from the disk, and runs sysprep, which shuts the
# VM down.
# This script holds no secret and no rendered value: the management public key is read from
# management.pub on the seed ISO.

$ErrorActionPreference = 'Stop'
Start-Transcript -Path C:\Windows\Temp\first-logon.log

Write-Output 'Step 1: replace the one-time password'
# The seed ISO carried the one-time password, so it must not stay a working credential on the
# template or its clones: the built-in Administrator (found by its SID, which ends in -500) gets
# a random password generated here and never written or printed. Each clone's administrator
# password is to be set from the secret store at provisioning (design D3, task 4.2; not built yet).
$Admin = Get-LocalUser | Where-Object { $_.SID.Value -like 'S-1-5-*-500' }
$Bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($Bytes)
Set-LocalUser -SID $Admin.SID -Password (ConvertTo-SecureString ([Convert]::ToBase64String($Bytes)) -AsPlainText -Force)
Remove-Variable Bytes

Write-Output 'Step 2: find the seed ISO and the VirtIO ISO'
# Drive letters are not fixed, so each ISO is found by the files it holds.
$Roots = Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Root.TrimEnd('\') }
$Seed = $Roots | Where-Object { (Test-Path "$_\first-logon.ps1") -and (Test-Path "$_\clone-unattend.xml") -and (Test-Path "$_\management.pub") } | Select-Object -First 1
# The guest-agent\qemu-ga-x86_64.msi path: confirm on the pinned VirtIO ISO (spike U09).
$VirtIO = $Roots | Where-Object { Test-Path "$_\guest-agent\qemu-ga-x86_64.msi" } | Select-Object -First 1
if (-not $Seed) { throw 'Seed ISO not found: no drive holds first-logon.ps1, clone-unattend.xml and management.pub.' }
if (-not $VirtIO) { throw 'VirtIO ISO not found: no drive holds guest-agent\qemu-ga-x86_64.msi.' }

Write-Output 'Step 3: install the QEMU guest agent'
# The agent needs the vioserial driver, which the answer file loads through DriverPaths. If the
# spike finds that driver missing, the fix is the virtio-win guest tools installer (spike U09).
$p = Start-Process msiexec.exe -ArgumentList '/i', "$VirtIO\guest-agent\qemu-ga-x86_64.msi", '/qn', '/norestart' -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "QEMU guest agent install failed with exit code $($p.ExitCode)." }

Write-Output 'Step 4: enable OpenSSH with PowerShell as the default shell'
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-Item -Path HKLM:\SOFTWARE\OpenSSH -Force | Out-Null
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null

Write-Output 'Step 5: install the management public key'
# sshd reads administrators' keys from this file only when Administrators and SYSTEM alone hold access.
$Auth = 'C:\ProgramData\ssh\administrators_authorized_keys'
Copy-Item "$Seed\management.pub" $Auth -Force
icacls.exe $Auth /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F'
if ($LASTEXITCODE -ne 0) { throw "icacls failed with exit code $LASTEXITCODE." }

Write-Output 'Step 6: apply the baseline (no RDP, no SMB inbound, no WinRM listener)'
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 1
Disable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
Disable-NetFirewallRule -DisplayGroup 'File and Printer Sharing' -ErrorAction SilentlyContinue
Get-ChildItem WSMan:\localhost\Listener -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Stop-Service WinRM -Force -ErrorAction SilentlyContinue
Set-Service WinRM -StartupType Disabled

Write-Output 'Step 7: remove the one-time password from the disk'
# Where Windows Setup caches the answer file is unverified; each known place is removed, and the
# spike reads the template disk for the password after sysprep (spike U09).
Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -Name DefaultPassword,AutoAdminLogon,AutoLogonCount -ErrorAction SilentlyContinue
Remove-Item -Path C:\Windows\Panther\unattend.xml, C:\Windows\Panther\Unattend\*, C:\Windows\System32\Sysprep\Panther\unattend.xml -Force -ErrorAction SilentlyContinue

Write-Output 'Step 8: stage the clone answer file'
Copy-Item "$Seed\clone-unattend.xml" C:\Windows\System32\Sysprep\clone-unattend.xml -Force

Write-Output 'Step 9: generalize the template and shut down'
Stop-Transcript
& "$env:SystemRoot\System32\Sysprep\sysprep.exe" /generalize /oobe /shutdown /quiet /unattend:C:\Windows\System32\Sysprep\clone-unattend.xml
