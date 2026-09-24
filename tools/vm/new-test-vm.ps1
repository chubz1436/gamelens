# Creates the GameLens test VM: Hyper-V, Windows guest, RTX 4060 shared by GPU partitioning.
# Run in an ELEVATED PowerShell. Nothing here has been run yet; it is written for the Owner.
#
#   Step 1 (once, reboots):  Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All
#   Step 2:                  .\new-test-vm.ps1 -Iso D:\path\Win10_22H2_English_x64.iso
#   Step 3:                  install Windows in the VM (local account is fine), shut it down
#   Step 4:                  .\new-test-vm.ps1 -CopyDrivers
#   Step 5:                  start the VM, install Minecraft from the Store, sign in (Owner only)
param(
    [string]$Name = 'GameLens-Test',
    [string]$Iso,
    [string]$Root = 'B:\VMs',
    [int]$MemoryGB = 8,
    [int]$Cpus = 4,
    [int]$DiskGB = 80,
    [switch]$CopyDrivers
)
$ErrorActionPreference = 'Stop'

if (-not (Get-Command New-VM -ErrorAction SilentlyContinue)) {
    throw 'Hyper-V is not enabled. Run step 1 (elevated) and reboot first.'
}

$vhd = Join-Path $Root "$Name\$Name.vhdx"

if ($CopyDrivers) {
    # GPU-P needs the host's own driver files inside the guest, at the paths the host uses.
    if ((Get-VM $Name).State -ne 'Off') { throw "Shut $Name down first." }
    $disk = Mount-VHD $vhd -PassThru | Get-Disk
    try {
        $win = Get-Partition -DiskNumber $disk.Number | Get-Volume |
            Where-Object { $_.DriveLetter -and (Test-Path "$($_.DriveLetter):\Windows\System32") } |
            Select-Object -First 1
        if (-not $win) { throw 'No Windows volume found in the VHD — is Windows installed?' }
        $g = "$($win.DriveLetter):"
        $store = "$g\Windows\System32\HostDriverStore\FileRepository"
        New-Item -ItemType Directory -Force $store | Out-Null
        $nv = Get-CimInstance Win32_VideoController | Where-Object Name -like '*NVIDIA*'
        if (-not $nv) { throw 'No NVIDIA adapter on the host.' }
        # The driver's INF folder is the parent of its installed .inf in the DriverStore.
        $inf = (Get-CimInstance Win32_PnPSignedDriver | Where-Object DeviceName -like '*NVIDIA GeForce*' |
            Select-Object -First 1).InfName
        $pkg = Get-ChildItem 'C:\Windows\System32\DriverStore\FileRepository' -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName 'nvlddmkm.sys') } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $pkg) { throw "Could not find the NVIDIA driver package (inf $inf)." }
        Copy-Item $pkg.FullName $store -Recurse -Force
        Get-ChildItem 'C:\Windows\System32' -Filter 'nv*' -File | Copy-Item -Destination "$g\Windows\System32" -Force
        Write-Host "Copied $($pkg.Name) and System32\nv* into the guest. Redo this after every host driver update."
    } finally {
        Dismount-VHD $vhd
    }
    return
}

if (-not $Iso -or -not (Test-Path $Iso)) { throw 'Pass -Iso with a Windows 10/11 x64 ISO.' }
if (Get-VM $Name -ErrorAction SilentlyContinue) { throw "$Name already exists." }

New-Item -ItemType Directory -Force (Split-Path $vhd) | Out-Null
New-VM -Name $Name -Generation 2 -MemoryStartupBytes ($MemoryGB * 1GB) -NewVHDPath $vhd `
    -NewVHDSizeBytes ($DiskGB * 1GB) -SwitchName 'Default Switch' -Path $Root | Out-Null
Set-VM $Name -ProcessorCount $Cpus -StaticMemory -CheckpointType Disabled `
    -AutomaticStopAction ShutDown -GuestControlledCacheTypes $true `
    -LowMemoryMappedIoSpace 1GB -HighMemoryMappedIoSpace 32GB
Add-VMDvdDrive $Name -Path $Iso
Set-VMFirmware $Name -FirstBootDevice (Get-VMDvdDrive $Name)
Set-VMKeyProtector $Name -NewLocalKeyProtector
Enable-VMTPM $Name

# Windows 10 cannot choose which GPU is partitioned; it takes the default one. With the AMD
# iGPU also present, check Get-VMPartitionableGpu lists the RTX 4060 — if it lists the AMD,
# disable the iGPU in BIOS (or Device Manager) before this step.
Get-VMPartitionableGpu | Format-List Name
Add-VMGpuPartitionAdapter $Name
Set-VMGpuPartitionAdapter $Name -MinPartitionVRAM 80000000 -MaxPartitionVRAM 100000000 -OptimalPartitionVRAM 100000000 `
    -MinPartitionEncode 80000000 -MaxPartitionEncode 100000000 -OptimalPartitionEncode 100000000 `
    -MinPartitionDecode 80000000 -MaxPartitionDecode 100000000 -OptimalPartitionDecode 100000000 `
    -MinPartitionCompute 80000000 -MaxPartitionCompute 100000000 -OptimalPartitionCompute 100000000

Write-Host "Created $Name. Start it: vmconnect localhost $Name ; Start-VM $Name"
