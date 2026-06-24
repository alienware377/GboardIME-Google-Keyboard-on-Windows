// GboardIME Setup.exe — thin native launcher.
//
// Lives in the repo root and runs the sibling install.ps1 with a Bypass execution
// policy, so end users get a familiar double-clickable .exe instead of having to
// invoke PowerShell themselves. All real work lives in install.ps1 (single source
// of truth); this just locates it next to the .exe and launches it.
//
// Build (no external tooling needed):
//   csc.exe /target:exe /out:Setup.exe /win32icon:setup\app.ico setup\Setup.cs
//   (icon optional)
//
// Debug: run `Setup.exe --print` to print the command it WOULD run, then exit.

using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

internal static class Setup
{
    private static int Main(string[] args)
    {
        // Directory the .exe lives in (the repo root).
        string exeDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string installPs1 = Path.Combine(exeDir, "install.ps1");

        // Pass through any extra args (e.g. -SkipDebloat), except our own --print flag.
        var passthrough = new StringBuilder();
        bool printOnly = false;
        foreach (string a in args)
        {
            if (string.Equals(a, "--print", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(a, "/print", StringComparison.OrdinalIgnoreCase))
            {
                printOnly = true;
                continue;
            }
            passthrough.Append(' ').Append(a);
        }

        string psArgs = string.Format(
            "-NoProfile -ExecutionPolicy Bypass -File \"{0}\"{1}",
            installPs1, passthrough.ToString());

        if (printOnly)
        {
            Console.WriteLine("powershell.exe " + psArgs);
            Console.WriteLine("(install.ps1 expected at: " + installPs1 + ")");
            Console.WriteLine("exists: " + File.Exists(installPs1));
            return 0;
        }

        if (!File.Exists(installPs1))
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine("ERROR: install.ps1 was not found next to Setup.exe.");
            Console.ResetColor();
            Console.WriteLine("Keep Setup.exe inside the GboardIME folder (don't move it out on its own).");
            Console.WriteLine("Expected: " + installPs1);
            Console.WriteLine();
            Console.WriteLine("Press any key to close.");
            Console.ReadKey();
            return 1;
        }

        Console.WriteLine("Starting GboardIME installer...");
        Console.WriteLine();

        var psi = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = psArgs,
            UseShellExecute = false,   // inherit this console so the user sees progress
            WorkingDirectory = exeDir
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
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine("Failed to launch PowerShell: " + ex.Message);
            Console.ResetColor();
            exit = 1;
        }

        Console.WriteLine();
        Console.WriteLine("Installer finished (exit code " + exit + "). Press any key to close.");
        Console.ReadKey();
        return exit;
    }
}
