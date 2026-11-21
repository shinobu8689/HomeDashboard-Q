"""Read local CPU, GPU, and memory usage for the dashboard."""

import json
import os
import platform
import subprocess
from functools import lru_cache
from typing import TypedDict

import psutil


class GPUDevice(TypedDict):
    luid: str
    name: str
    dedicated: bool


GPU_NAMES_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$source = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class DisplayAdapters {
  [StructLayout(LayoutKind.Sequential)] public struct LUID {
    public uint LowPart;
    public int HighPart;
    public override string ToString() {
      return String.Format("0x{0:x8}_0x{1:x8}", unchecked((uint)HighPart), LowPart);
    }
  }
  [StructLayout(LayoutKind.Sequential)] public struct SOURCE {
    public LUID adapterId;
    public uint id, modeInfoIdx, statusFlags;
  }
  [StructLayout(LayoutKind.Sequential)] public struct RATIONAL {
    public uint Numerator, Denominator;
  }
  [StructLayout(LayoutKind.Sequential)] public struct TARGET {
    public LUID adapterId;
    public uint id, modeInfoIdx, outputTechnology, rotation, scaling;
    public RATIONAL refreshRate;
    public uint scanLineOrdering;
    [MarshalAs(UnmanagedType.Bool)] public bool targetAvailable;
    public uint statusFlags;
  }
  [StructLayout(LayoutKind.Sequential)] public struct PATH {
    public SOURCE source;
    public TARGET target;
    public uint flags;
  }
  [DllImport("user32.dll")] public static extern int GetDisplayConfigBufferSizes(
    uint flags, out uint paths, out uint modes);
  [DllImport("user32.dll")] public static extern int QueryDisplayConfig(
    uint flags, ref uint paths, [Out] PATH[] pathArray, ref uint modes,
    IntPtr modeArray, IntPtr topologyId);
  [DllImport("user32.dll")] public static extern int DisplayConfigGetDeviceInfo(
    IntPtr requestPacket);
  public static string[] GetAdapterPaths() {
    uint pathCount, modeCount;
    int result = GetDisplayConfigBufferSizes(1, out pathCount, out modeCount);
    if (result != 0) throw new InvalidOperationException("Could not query display adapters: " + result);
    PATH[] paths = new PATH[pathCount];
    IntPtr modes = Marshal.AllocHGlobal((int)(modeCount * 64));
    try {
      result = QueryDisplayConfig(1, ref pathCount, paths, ref modeCount, modes, IntPtr.Zero);
      if (result != 0) throw new InvalidOperationException("Could not query display adapter paths: " + result);
      HashSet<string> adapters = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
      foreach (PATH path in paths) {
        IntPtr request = Marshal.AllocHGlobal(276);
        try {
          for (int i = 0; i < 276; i++) Marshal.WriteByte(request, i, 0);
          Marshal.WriteInt32(request, 0, 4);
          Marshal.WriteInt32(request, 4, 276);
          Marshal.StructureToPtr(path.source.adapterId, IntPtr.Add(request, 8), false);
          Marshal.WriteInt32(request, 16, (int)path.source.id);
          result = DisplayConfigGetDeviceInfo(request);
          if (result != 0) continue;
          string devicePath = Marshal.PtrToStringUni(IntPtr.Add(request, 20));
          if (!String.IsNullOrEmpty(devicePath))
            adapters.Add(path.source.adapterId.ToString() + "|" + devicePath);
        } finally {
          Marshal.FreeHGlobal(request);
        }
      }
      return new List<string>(adapters).ToArray();
    } finally {
      Marshal.FreeHGlobal(modes);
    }
  }
}
'@
Add-Type -TypeDefinition $source
$adapterPaths = [DisplayAdapters]::GetAdapterPaths()
$devices = foreach ($device in (Get-CimInstance -ClassName Win32_VideoController |
    Where-Object { $_.PNPDeviceID -notlike 'ROOT\*' })) {
    $devicePath = $device.PNPDeviceID.Replace('\', '#')
    $adapter = $adapterPaths | Where-Object {
        $path = ($_ -split '\|', 2)[1] -replace '^\\\\\?\\', ''
        $path = $path -replace '#\{.*$', ''
        $path.StartsWith($devicePath, [StringComparison]::OrdinalIgnoreCase)
    } | Select-Object -First 1
    if ($adapter) {
        [PSCustomObject]@{
            luid = ($adapter -split '\|', 2)[0]
            name = $device.Name
            dedicated = $device.Name -match '(?i)NVIDIA|AMD|Radeon'
        }
    }
}
ConvertTo-Json -InputObject @($devices) -Compress
"""

GPU_USAGE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$usageSamples = foreach ($sample in (Get-Counter -Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples) {
    if ($sample.Status -eq 0 -and $sample.InstanceName -match 'luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_3d') {
        [PSCustomObject]@{
            adapter = "$($Matches[1])_$($Matches[2])"
            physicalEngine = [int]$Matches[3]
            engine = [int]$Matches[4]
            utilization = [double]$sample.CookedValue
        }
    }
}
if (-not $usageSamples) {
    throw 'No GPU 3D utilization counters are available'
}
$results = foreach ($adapterGroup in ($usageSamples | Group-Object adapter)) {
    $engineUsage = @{}
    foreach ($sample in $adapterGroup.Group) {
        $key = "$($sample.physicalEngine)_$($sample.engine)"
        $engineUsage[$key] = [double]($engineUsage[$key] + $sample.utilization)
    }
    [PSCustomObject]@{
        luid = $adapterGroup.Name
        percent = [Math]::Min(100, [Math]::Max(0, ($engineUsage.Values | Measure-Object -Maximum).Maximum))
    }
}
ConvertTo-Json -InputObject @($results) -Compress
"""


@lru_cache(maxsize=1)
def read_hardware_names() -> tuple[str | None, str | None, list[GPUDevice], str | None]:
    cpu_name = platform.processor().strip() or None
    cpu_name_error = None if cpu_name else "CPU model information is unavailable"
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as processor_key:
                cpu_name = winreg.QueryValueEx(processor_key, "ProcessorNameString")[0].strip()
                cpu_name_error = None
        except (OSError, ImportError) as error:
            cpu_name_error = f"Could not read CPU model: {error}"

    if os.name != "nt":
        return cpu_name, cpu_name_error, [], "GPU model detection is only available on Windows"

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                GPU_NAMES_SCRIPT,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return cpu_name, cpu_name_error, [], f"Could not read GPU model: {error}"

    if result.returncode != 0:
        details = result.stderr.strip() or "GPU model information is unavailable"
        return cpu_name, cpu_name_error, [], details.splitlines()[-1]

    try:
        devices = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return cpu_name, cpu_name_error, [], f"Could not parse GPU model information: {error}"

    if isinstance(devices, dict):
        devices = [devices]
    if not isinstance(devices, list):
        return cpu_name, cpu_name_error, [], "GPU model information returned an unexpected response"

    gpu_devices: list[GPUDevice] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        luid = device.get("luid")
        name = device.get("name")
        if isinstance(luid, str) and isinstance(name, str):
            gpu_devices.append({
                "luid": luid,
                "name": name,
                "dedicated": device.get("dedicated") is True,
            })
    if not gpu_devices:
        return cpu_name, cpu_name_error, [], "No GPU name could be matched to a Windows GPU adapter"
    return cpu_name, cpu_name_error, gpu_devices, None


def read_gpu_usage(
    gpu_devices: list[GPUDevice],
) -> tuple[float | None, str | None, str | None]:
    if os.name != "nt":
        return None, None, "GPU utilization is only available on Windows"

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                GPU_USAGE_SCRIPT,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, None, f"Could not read GPU utilization: {error}"

    if result.returncode != 0:
        details = result.stderr.strip() or "GPU utilization counters are unavailable"
        return None, None, details.splitlines()[-1]

    try:
        samples = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return None, None, f"Could not parse GPU utilization counters: {error}"

    if isinstance(samples, dict):
        samples = [samples]
    if not isinstance(samples, list) or not samples:
        return None, None, "GPU utilization counters returned no data"

    usage_by_luid: dict[str, float] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        try:
            luid = str(sample["luid"])
            utilization = float(sample["percent"])
        except (KeyError, TypeError, ValueError):
            continue
        if not luid:
            continue
        usage_by_luid[luid] = max(usage_by_luid.get(luid, 0.0), utilization)

    if not usage_by_luid:
        return None, None, "GPU utilization counters returned no valid data"

    matched_devices = [
        device for device in gpu_devices if device["luid"] in usage_by_luid
    ]
    if not matched_devices:
        return None, None, "GPU usage counters could not be matched to a GPU name"

    selected_device = next(
        (device for device in matched_devices if device.get("dedicated")),
        matched_devices[0],
    )
    return (
        min(100.0, max(0.0, usage_by_luid[selected_device["luid"]])),
        selected_device["name"],
        None,
    )


def read_system_usage() -> dict[str, float | str | None]:
    memory = psutil.virtual_memory()
    cpu_name, cpu_name_error, gpu_devices, gpu_name_error = read_hardware_names()
    gpu_percent, gpu_name, gpu_error = read_gpu_usage(gpu_devices)
    return {
        "cpuName": cpu_name,
        "cpuNameError": cpu_name_error,
        "gpuName": gpu_name,
        "gpuNameError": gpu_name_error or gpu_error,
        "cpuPercent": round(psutil.cpu_percent(interval=0.15), 1),
        "gpuPercent": round(gpu_percent, 1) if gpu_percent is not None else None,
        "gpuError": gpu_error,
        "memoryUsedGb": round((memory.total - memory.available) / (1024 ** 3), 1),
        "memoryTotalGb": round(memory.total / (1024 ** 3), 1),
        "memoryPercent": round(memory.percent, 1),
    }
