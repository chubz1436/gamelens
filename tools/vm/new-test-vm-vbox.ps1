# Creates the GameLens test VM in VirtualBox: Windows 11 guest, EFI + Secure Boot + TPM 2.0.
# No admin, no reboot. Leaves every other VirtualBox VM alone (it only touches -Name).
#
# No real GPU: 3D is VirtualBox's software/SVGA path, and with Hyper-V's platform active on the
# host (WSL2/Docker) VirtualBox runs on top of it, which is slower still. Good for driving
# GameLens itself and 2D games; Minecraft Bedrock may be too slow. See new-test-vm.ps1 for GPU-P.
#
#   .\new-test-vm-vbox.ps1 -Iso B:\VMs\iso\Win11_25H2_English_x64_v2.iso
#   then install Windows in the VM window (the Owner creates the account)
param(
    [string]$Name = 'GameLens-Test',
    [Parameter(Mandatory)][string]$Iso,
    [string]$Root = 'B:\VMs',
    [int]$MemoryGB = 8,
    [int]$Cpus = 4,   # 6 hangs the EFI firmware (PciHostBridgeDxe) under NEM; 1-4 boot
    [int]$DiskGB = 80,
    [int]$VramMB = 256
)
$ErrorActionPreference = 'Stop'

$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
if (-not (Test-Path $vbox)) { throw 'VirtualBox is not installed.' }
function vbm { & $vbox @args; if ($LASTEXITCODE) { throw "VBoxManage $($args[0]) failed ($LASTEXITCODE)" } }

if (-not (Test-Path $Iso)) { throw "No ISO at $Iso" }
if ((& $vbox list vms) -match "^`"$([regex]::Escape($Name))`" ") { throw "$Name already exists." }

$disk = Join-Path $Root "$Name\$Name.vdi"
vbm createvm --name $Name --ostype Windows11_64 --basefolder $Root --register
# 3D stays off: with it on, Win11 setup hangs on a black screen after its first reboot.
vbm modifyvm $Name --memory ($MemoryGB * 1024) --cpus $Cpus --vram $VramMB `
    --graphicscontroller vboxsvga --accelerate-3d off `
    --firmware efi --tpm-type 2.0 --chipset piix3 `
    --nic1 nat --audio-enabled off --clipboard-mode disabled --drag-and-drop disabled `
    --mouse usbtablet --usb-xhci on
vbm createmedium disk --filename $disk --size ($DiskGB * 1024) --format VDI
vbm storagectl $Name --name SATA --add sata --controller IntelAhci --portcount 2
vbm storageattach $Name --storagectl SATA --port 0 --device 0 --type hdd --medium $disk
vbm storageattach $Name --storagectl SATA --port 1 --device 0 --type dvddrive --medium $Iso
vbm modifynvram $Name inituefivarstore
vbm modifynvram $Name enrollmssignatures
vbm modifynvram $Name enrollorclpk

# GameLens in the guest binds 127.0.0.1:8777, so a NAT rule to it cannot land. Forward the
# guest's OpenSSH instead (host loopback only) and tunnel:  ssh -p 2222 -L 8777:127.0.0.1:8777 <user>@127.0.0.1
# The tunnel keeps GameLens's Host/Origin check (127.0.0.1:8777) satisfied without code changes.
vbm modifyvm $Name --natpf1 "ssh,tcp,127.0.0.1,2222,,22"

Write-Host "Created $Name. Start: & '$vbox' startvm $Name"
