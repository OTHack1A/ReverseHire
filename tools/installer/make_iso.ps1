# Builds an ISO image (UDF) from a prepared folder, using the image writer that
# ships with Windows (IMAPI2). No extra tools are needed.
#
#   powershell -ExecutionPolicy Bypass -File tools\installer\make_iso.ps1 `
#       -Source dist\iso_staging -Output dist\ReverseHire-Setup.iso
#
# The content of the staging folder is described in docs/INSTALLATION.md
# ("Rebuilding the installation ISO").

param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Label = 'REVERSEHIRE'
)

$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path $Source).Path
$Output = [IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class IsoStreamWriter
{
    // Copies the COM stream produced by IMAPI2 to a file.
    public static void Save(object comStream, string path, int blockSize, int totalBlocks)
    {
        IStream stream = (IStream)comStream;
        byte[] buffer = new byte[blockSize * 512];
        IntPtr read = Marshal.AllocHGlobal(sizeof(int));
        try
        {
            using (FileStream file = File.Create(path))
            {
                long remaining = (long)blockSize * totalBlocks;
                while (remaining > 0)
                {
                    stream.Read(buffer, buffer.Length, read);
                    int count = Marshal.ReadInt32(read);
                    if (count <= 0) break;
                    file.Write(buffer, 0, count);
                    remaining -= count;
                }
            }
        }
        finally
        {
            Marshal.FreeHGlobal(read);
        }
    }
}
'@

$image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$image.ChooseImageDefaultsForMediaType(6)   # single-layer DVD (4.7 GB)
$image.FileSystemsToCreate = 4              # UDF: long file names, large files
$image.VolumeName = $Label
Write-Host "Adding $Source ..."
$image.Root.AddTree($Source, $false)
$result = $image.CreateResultImage()

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null
Write-Host "Writing $Output ..."
[IsoStreamWriter]::Save($result.ImageStream, $Output, $result.BlockSize, $result.TotalBlocks)

$size = (Get-Item $Output).Length
Write-Host ("Done: {0:N0} MB" -f ($size / 1MB))
