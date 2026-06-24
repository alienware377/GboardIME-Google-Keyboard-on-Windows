// GboardIME Setup.exe - self-contained installer.
//
// Bundles the ENTIRE project (install.ps1, launch/stop scripts, the relay APK,
// the Windows host, android sources, debloat list) as an embedded zip resource.
// On run it extracts everything to %LOCALAPPDATA%\Programs\GboardIME and launches
// install.ps1 from there - so a single .exe is all the user needs, no extra files.
//
// Build: see setup\build-setup.ps1 (compiles with the in-box .NET csc.exe and
// embeds setup\payload.zip as the resource "GboardIME.payload.zip").
//
// Flags:
//   --dir <path>   extract to a custom folder (default %LOCALAPPDATA%\Programs\GboardIME)
//   --extract-only unpack but don't run the installer
//   anything else  passed through to install.ps1 (e.g. -SkipDebloat)

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;

internal static class Setup
{
    private const string ResourceName = "GboardIME.payload.zip";

    private static int Main(string[] args)
    {
        string targetDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs", "GboardIME");
        bool extractOnly = false;
        var passthrough = new StringBuilder();

        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (a.Equals("--dir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                targetDir = args[++i];
            }
            else if (a.Equals("--extract-only", StringComparison.OrdinalIgnoreCase))
            {
                extractOnly = true;
            }
            else
            {
                passthrough.Append(' ').Append(a);
            }
        }

        Console.WriteLine("GboardIME setup");
        Console.WriteLine("===============");
        Console.WriteLine("Install folder: " + targetDir);
        Console.WriteLine();

        try
        {
            ExtractPayload(targetDir);
        }
        catch (Exception ex)
        {
            Fail("Could not unpack the bundled files: " + ex.Message);
            return 1;
        }

        string installPs1 = Path.Combine(targetDir, "install.ps1");
        if (!File.Exists(installPs1))
        {
            Fail("install.ps1 missing after extraction (corrupt build?).");
            return 1;
        }

        if (extractOnly)
        {
            Console.WriteLine("Extracted to " + targetDir + " (--extract-only; installer not run).");
            return 0;
        }

        Console.WriteLine("Files unpacked. Starting installer...");
        Console.WriteLine();

        var psi = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = string.Format(
                "-NoProfile -ExecutionPolicy Bypass -File \"{0}\"{1}",
                installPs1, passthrough.ToString()),
            UseShellExecute = false,
            WorkingDirectory = targetDir
        };

        int exit;
        try
        {
            using (Process p = Process.Start(psi))
            {
                p.WaitForExit();
                exit = p.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Fail("Failed to launch PowerShell: " + ex.Message);
            return 1;
        }

        Console.WriteLine();
        Console.WriteLine("Installer finished (exit code " + exit + "). Press any key to close.");
        Console.ReadKey();
        return exit;
    }

    private static void ExtractPayload(string targetDir)
    {
        Directory.CreateDirectory(targetDir);
        Assembly asm = Assembly.GetExecutingAssembly();
        using (Stream res = asm.GetManifestResourceStream(ResourceName))
        {
            if (res == null)
                throw new Exception("embedded payload not found (" + ResourceName + ")");
            using (var zip = new ZipArchive(res, ZipArchiveMode.Read))
            {
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    string outPath = Path.Combine(targetDir, entry.FullName.Replace('/', '\\'));
                    if (entry.FullName.EndsWith("/") || string.IsNullOrEmpty(entry.Name))
                    {
                        Directory.CreateDirectory(outPath);
                        continue;
                    }
                    string dir = Path.GetDirectoryName(outPath);
                    if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                    entry.ExtractToFile(outPath, true);  // overwrite on re-run
                }
            }
        }
    }

    private static void Fail(string msg)
    {
        Console.ForegroundColor = ConsoleColor.Red;
        Console.WriteLine("ERROR: " + msg);
        Console.ResetColor();
        Console.WriteLine();
        Console.WriteLine("Press any key to close.");
        Console.ReadKey();
    }
}
