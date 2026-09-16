using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Autosport.Release
{
    public static class BirthProtectedPyInstaller
    {
        private const uint PROCESS_CREATE_THREAD = 0x00000002;
        private const uint PROCESS_VM_OPERATION = 0x00000008;
        private const uint PROCESS_VM_WRITE = 0x00000020;
        private const uint PROCESS_DUP_HANDLE = 0x00000040;
        private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000;
        private const uint WRITE_DAC = 0x00040000;
        private const uint WRITE_OWNER = 0x00080000;
        private const uint SYNCHRONIZE = 0x00100000;
        private const uint DANGEROUS_PROCESS_ACCESS = 0x000C006A;
        private const uint SAFE_PARENT_ACCESS = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE;

        private const uint THREAD_SUSPEND_RESUME = 0x00000002;
        private const uint THREAD_SET_CONTEXT = 0x00000010;
        private const uint THREAD_QUERY_LIMITED_INFORMATION = 0x00000800;
        private const uint DANGEROUS_THREAD_ACCESS = 0x000C17B3;
        private const uint SAFE_THREAD_ACCESS = THREAD_QUERY_LIMITED_INFORMATION | SYNCHRONIZE;

        private const uint TOKEN_ASSIGN_PRIMARY = 0x0001;
        private const uint TOKEN_DUPLICATE = 0x0002;
        private const uint TOKEN_QUERY = 0x0008;
        private const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
        private const uint DISABLE_MAX_PRIVILEGE = 0x00000001;
        private const int ERROR_ACCESS_DENIED = 5;
        private const int ERROR_NOT_ALL_ASSIGNED = 1300;
        private const uint WAIT_OBJECT_0 = 0x00000000;
        private const uint INFINITE = 0xFFFFFFFF;
        private const uint SDDL_REVISION_1 = 1;
        private const string EVERYONE_SID = "S-1-1-0";

        private const string ProtectedWorkerArgument = "--autosport-birth-protected-worker";
        private const string BarrierEnvironment = "AUTOSPORT_BINDER_LAUNCH_BARRIER";
        private const string NonceEnvironment = "AUTOSPORT_BINDER_LAUNCH_NONCE";
        private const string TestSiblingProbeEnvironment =
            "AUTOSPORT_TEST_ORCHESTRATOR_BIRTH_PROCESS_SIBLING_PROBE";

        private const string SiblingProbe =
            "import ctypes,sys\n" +
            "from ctypes import wintypes\n" +
            "TOKEN_ADJUST_PRIVILEGES=0x20; TOKEN_QUERY=0x8; ERROR_NOT_ALL_ASSIGNED=1300; ERROR_ACCESS_DENIED=5\n" +
            "class LUID(ctypes.Structure): _fields_=(('LowPart',wintypes.DWORD),('HighPart',wintypes.LONG))\n" +
            "class LAA(ctypes.Structure): _fields_=(('Luid',LUID),('Attributes',wintypes.DWORD))\n" +
            "class TP(ctypes.Structure): _fields_=(('PrivilegeCount',wintypes.DWORD),('Privileges',LAA*1))\n" +
            "k=ctypes.WinDLL('kernel32',use_last_error=True); a=ctypes.WinDLL('advapi32',use_last_error=True)\n" +
            "k.GetCurrentProcess.restype=wintypes.HANDLE; k.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD); k.OpenProcess.restype=wintypes.HANDLE; k.CloseHandle.argtypes=(wintypes.HANDLE,)\n" +
            "a.OpenProcessToken.argtypes=(wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)); a.LookupPrivilegeValueW.argtypes=(wintypes.LPCWSTR,wintypes.LPCWSTR,ctypes.POINTER(LUID)); a.AdjustTokenPrivileges.argtypes=(wintypes.HANDLE,wintypes.BOOL,ctypes.POINTER(TP),wintypes.DWORD,ctypes.c_void_p,ctypes.c_void_p)\n" +
            "t=wintypes.HANDLE();\n" +
            "assert a.OpenProcessToken(k.GetCurrentProcess(),TOKEN_ADJUST_PRIVILEGES|TOKEN_QUERY,ctypes.byref(t))\n" +
            "l=LUID(); assert a.LookupPrivilegeValueW(None,'SeDebugPrivilege',ctypes.byref(l)); s=TP(); s.PrivilegeCount=1; s.Privileges[0].Luid=l; s.Privileges[0].Attributes=0; ctypes.set_last_error(0); assert a.AdjustTokenPrivileges(t,False,ctypes.byref(s),0,None,None); e=ctypes.get_last_error(); k.CloseHandle(t)\n" +
            "if e==0: raise SystemExit('SEDEBUG_ASSIGNED')\n" +
            "if e!=ERROR_NOT_ALL_ASSIGNED: raise SystemExit(f'SEDEBUG_PROBE_FAILED:{e}')\n" +
            "pid=int(sys.argv[1]); rights=(0x2,0x8,0x20,0x40,0x40000,0x80000)\n" +
            "for access in rights:\n" +
            " ctypes.set_last_error(0); h=k.OpenProcess(access,False,pid); v=h if isinstance(h,int) else ctypes.cast(h,ctypes.c_void_p).value\n" +
            " if v: k.CloseHandle(h); raise SystemExit(f'DANGEROUS_ACCESS_AVAILABLE:{access}')\n" +
            " if ctypes.get_last_error()!=ERROR_ACCESS_DENIED: raise SystemExit(f'OPEN_PROCESS_FAILED:{access}:{ctypes.get_last_error()}')\n" +
            "print('DENIED',flush=True)\n";

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int nLength;
            public IntPtr lpSecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)]
            public bool bInheritHandle;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct Luid
        {
            public uint LowPart;
            public int HighPart;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LuidAndAttributes
        {
            public Luid Luid;
            public uint Attributes;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TokenPrivileges
        {
            public uint PrivilegeCount;
            public LuidAndAttributes Privileges;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SidAndAttributes
        {
            public IntPtr Sid;
            public uint Attributes;
        }

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(
            string stringSecurityDescriptor,
            uint stringSDRevision,
            out IntPtr securityDescriptor,
            out uint securityDescriptorSize);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ConvertStringSidToSidW(
            string stringSid,
            out IntPtr sid);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool OpenProcessToken(
            IntPtr processHandle,
            uint desiredAccess,
            out IntPtr tokenHandle);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool LookupPrivilegeValueW(
            string systemName,
            string name,
            out Luid luid);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AdjustTokenPrivileges(
            IntPtr tokenHandle,
            [MarshalAs(UnmanagedType.Bool)] bool disableAllPrivileges,
            ref TokenPrivileges newState,
            uint bufferLength,
            IntPtr previousState,
            IntPtr returnLength);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateRestrictedToken(
            IntPtr existingTokenHandle,
            uint flags,
            uint disableSidCount,
            IntPtr sidsToDisable,
            uint deletePrivilegeCount,
            IntPtr privilegesToDelete,
            uint restrictedSidCount,
            IntPtr sidsToRestrict,
            out IntPtr newTokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsTokenRestricted(IntPtr tokenHandle);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcessAsUserW(
            IntPtr token,
            string applicationName,
            StringBuilder commandLine,
            ref SecurityAttributes processAttributes,
            ref SecurityAttributes threadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref StartupInfo startupInfo,
            out ProcessInformation processInformation);

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateEventW(
            IntPtr eventAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool manualReset,
            [MarshalAs(UnmanagedType.Bool)] bool initialState,
            string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(
            uint desiredAccess,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandle,
            uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenThread(
            uint desiredAccess,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandle,
            uint threadId);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetEvent(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr LocalFree(IntPtr memory);

        private static void RequireSeDebugNotAssigned()
        {
            IntPtr token;
            if (!OpenProcessToken(
                GetCurrentProcess(),
                TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                out token))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "orchestrator token cannot be inspected for SeDebugPrivilege");
            }

            try
            {
                Luid debugLuid;
                if (!LookupPrivilegeValueW(null, "SeDebugPrivilege", out debugLuid))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "SeDebugPrivilege lookup failed");
                }

                TokenPrivileges state = new TokenPrivileges();
                state.PrivilegeCount = 1;
                state.Privileges = new LuidAndAttributes
                {
                    Luid = debugLuid,
                    Attributes = 0
                };
                if (!AdjustTokenPrivileges(
                    token,
                    false,
                    ref state,
                    0,
                    IntPtr.Zero,
                    IntPtr.Zero))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "SeDebugPrivilege assignment probe failed");
                }
                int error = Marshal.GetLastWin32Error();
                if (error == 0)
                {
                    throw new InvalidOperationException(
                        "orchestrator token has SeDebugPrivilege assigned; birth-DACL proof is not authoritative");
                }
                if (error != ERROR_NOT_ALL_ASSIGNED)
                {
                    throw new Win32Exception(
                        error,
                        "SeDebugPrivilege assignment probe returned an ambiguous result");
                }
            }
            finally
            {
                CloseHandle(token);
            }
        }

        private static IntPtr CreateRestrictedPrimaryToken(string currentUserSid)
        {
            IntPtr currentToken = IntPtr.Zero;
            IntPtr restrictedToken = IntPtr.Zero;
            IntPtr currentUser = IntPtr.Zero;
            IntPtr everyone = IntPtr.Zero;
            IntPtr restrictingArray = IntPtr.Zero;
            try
            {
                if (!OpenProcessToken(
                    GetCurrentProcess(),
                    TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
                    out currentToken))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "orchestrator primary token cannot be opened");
                }
                if (!ConvertStringSidToSidW(currentUserSid, out currentUser))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "current-user restricting SID conversion failed");
                }
                if (!ConvertStringSidToSidW(EVERYONE_SID, out everyone))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Everyone restricting SID conversion failed");
                }

                int itemSize = Marshal.SizeOf(typeof(SidAndAttributes));
                restrictingArray = Marshal.AllocHGlobal(itemSize * 2);
                Marshal.StructureToPtr(
                    new SidAndAttributes { Sid = currentUser, Attributes = 0 },
                    restrictingArray,
                    false);
                Marshal.StructureToPtr(
                    new SidAndAttributes { Sid = everyone, Attributes = 0 },
                    IntPtr.Add(restrictingArray, itemSize),
                    false);

                if (!CreateRestrictedToken(
                    currentToken,
                    DISABLE_MAX_PRIVILEGE,
                    0,
                    IntPtr.Zero,
                    0,
                    IntPtr.Zero,
                    2,
                    restrictingArray,
                    out restrictedToken))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "protected-worker restricted token creation failed");
                }
                if (restrictedToken == IntPtr.Zero || !IsTokenRestricted(restrictedToken))
                {
                    if (restrictedToken != IntPtr.Zero)
                    {
                        CloseHandle(restrictedToken);
                        restrictedToken = IntPtr.Zero;
                    }
                    throw new InvalidOperationException(
                        "protected-worker primary token lacks restricting SIDs");
                }
                IntPtr result = restrictedToken;
                restrictedToken = IntPtr.Zero;
                return result;
            }
            finally
            {
                if (restrictingArray != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(restrictingArray);
                }
                if (everyone != IntPtr.Zero)
                {
                    LocalFree(everyone);
                }
                if (currentUser != IntPtr.Zero)
                {
                    LocalFree(currentUser);
                }
                if (restrictedToken != IntPtr.Zero)
                {
                    CloseHandle(restrictedToken);
                }
                if (currentToken != IntPtr.Zero)
                {
                    CloseHandle(currentToken);
                }
            }
        }

        private static IntPtr SecurityDescriptor(
            string currentUserSid,
            uint deniedAccess,
            uint allowedAccess,
            string objectLabel)
        {
            if (String.IsNullOrWhiteSpace(currentUserSid) ||
                !currentUserSid.StartsWith("S-", StringComparison.Ordinal))
            {
                throw new ArgumentException(
                    "birth-protected launch requires a canonical current-user SID",
                    "currentUserSid");
            }

            string sddl =
                "D:P" +
                String.Format("(D;;0x{0:x8};;;{1})", deniedAccess, currentUserSid) +
                String.Format("(D;;0x{0:x8};;;OW)", deniedAccess) +
                String.Format("(A;;0x{0:x8};;;{1})", allowedAccess, currentUserSid) +
                "(A;;GA;;;SY)" +
                "(A;;GA;;;BA)";
            IntPtr descriptor;
            uint length;
            if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl,
                SDDL_REVISION_1,
                out descriptor,
                out length))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "unable to build " + objectLabel + " birth security descriptor");
            }
            if (descriptor == IntPtr.Zero || length == 0)
            {
                if (descriptor != IntPtr.Zero)
                {
                    LocalFree(descriptor);
                }
                throw new InvalidOperationException(
                    objectLabel + " birth security descriptor is empty");
            }
            return descriptor;
        }

        private static void RequireFreshProcessAccessDenied(uint pid)
        {
            uint[] rights = new uint[]
            {
                PROCESS_CREATE_THREAD,
                PROCESS_VM_OPERATION,
                PROCESS_VM_WRITE,
                PROCESS_DUP_HANDLE,
                WRITE_DAC,
                WRITE_OWNER
            };
            foreach (uint access in rights)
            {
                IntPtr handle = OpenProcess(access, false, pid);
                if (handle != IntPtr.Zero)
                {
                    CloseHandle(handle);
                    throw new InvalidOperationException(
                        String.Format(
                            "birth-protected worker still allows fresh process access 0x{0:x8}",
                            access));
                }
                int error = Marshal.GetLastWin32Error();
                if (error != ERROR_ACCESS_DENIED)
                {
                    throw new Win32Exception(
                        error,
                        String.Format(
                            "birth-protected process access probe 0x{0:x8} did not fail with access denied",
                            access));
                }
            }
        }

        private static void RequireFreshThreadAccessDenied(uint tid)
        {
            uint[] rights = new uint[]
            {
                THREAD_SET_CONTEXT,
                THREAD_SUSPEND_RESUME,
                WRITE_DAC,
                WRITE_OWNER
            };
            foreach (uint access in rights)
            {
                IntPtr handle = OpenThread(access, false, tid);
                if (handle != IntPtr.Zero)
                {
                    CloseHandle(handle);
                    throw new InvalidOperationException(
                        String.Format(
                            "birth-protected worker primary thread still allows fresh access 0x{0:x8}",
                            access));
                }
                int error = Marshal.GetLastWin32Error();
                if (error != ERROR_ACCESS_DENIED)
                {
                    throw new Win32Exception(
                        error,
                        String.Format(
                            "birth-protected thread access probe 0x{0:x8} did not fail with access denied",
                            access));
                }
            }
        }

        private static string QuoteArgument(string value)
        {
            if (value == null)
            {
                throw new ArgumentNullException("value");
            }
            if (value.Length > 0 &&
                value.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            {
                return value;
            }

            StringBuilder result = new StringBuilder();
            result.Append('"');
            int backslashes = 0;
            foreach (char character in value)
            {
                if (character == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (character == '"')
                {
                    result.Append('\\', backslashes * 2 + 1);
                    result.Append('"');
                    backslashes = 0;
                    continue;
                }
                result.Append('\\', backslashes);
                backslashes = 0;
                result.Append(character);
            }
            result.Append('\\', backslashes * 2);
            result.Append('"');
            return result.ToString();
        }

        private static string BuildCommandLine(
            string pythonExecutable,
            string launchBoundary,
            string binder,
            string[] binderArguments)
        {
            List<string> values = new List<string>();
            values.Add(pythonExecutable);
            values.Add("-I");
            values.Add(launchBoundary);
            values.Add(ProtectedWorkerArgument);
            values.Add(binder);
            if (binderArguments != null)
            {
                values.AddRange(binderArguments);
            }

            List<string> quoted = new List<string>();
            foreach (string value in values)
            {
                quoted.Add(QuoteArgument(value));
            }
            return String.Join(" ", quoted.ToArray());
        }

        private static void RunHostileSiblingProbe(string pythonExecutable, uint pid)
        {
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = pythonExecutable;
            info.UseShellExecute = false;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.CreateNoWindow = true;
            info.ArgumentList.Add("-I");
            info.ArgumentList.Add("-c");
            info.ArgumentList.Add(SiblingProbe);
            info.ArgumentList.Add(pid.ToString(System.Globalization.CultureInfo.InvariantCulture));

            using (Process probe = Process.Start(info))
            {
                if (probe == null)
                {
                    throw new InvalidOperationException(
                        "birth process hostile sibling probe could not start");
                }
                string stdout = probe.StandardOutput.ReadToEnd();
                string stderr = probe.StandardError.ReadToEnd();
                probe.WaitForExit();
                if (probe.ExitCode != 0 || stdout.Trim() != "DENIED")
                {
                    throw new InvalidOperationException(
                        "birth process hostile sibling probe failed closed: " +
                        (stdout + "\n" + stderr).Trim());
                }
            }
        }

        public static int Run(
            string pythonExecutable,
            string launchBoundary,
            string binder,
            string[] binderArguments,
            string workingDirectory,
            string currentUserSid)
        {
            if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
            {
                throw new PlatformNotSupportedException(
                    "birth-protected PyInstaller launch requires Windows");
            }
            if (!Path.IsPathRooted(pythonExecutable) || !File.Exists(pythonExecutable))
            {
                throw new ArgumentException(
                    "birth-protected launch requires an absolute Python executable",
                    "pythonExecutable");
            }
            if (!Path.IsPathRooted(launchBoundary) || !File.Exists(launchBoundary))
            {
                throw new ArgumentException(
                    "birth-protected launch requires an absolute launch-boundary path",
                    "launchBoundary");
            }
            if (!Path.IsPathRooted(binder) || !File.Exists(binder))
            {
                throw new ArgumentException(
                    "birth-protected launch requires an absolute binder path",
                    "binder");
            }
            if (String.IsNullOrWhiteSpace(workingDirectory) ||
                !Directory.Exists(workingDirectory))
            {
                throw new ArgumentException(
                    "birth-protected launch requires an existing working directory",
                    "workingDirectory");
            }

            RequireSeDebugNotAssigned();

            string nonce = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
            string barrierName =
                "Local\\AutosportBinderLaunch-" + Guid.NewGuid().ToString("N");
            IntPtr barrier = CreateEventW(
                IntPtr.Zero,
                true,
                false,
                barrierName);
            if (barrier == IntPtr.Zero)
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "unable to create protected binder launch barrier");
            }

            IntPtr processDescriptor = IntPtr.Zero;
            IntPtr threadDescriptor = IntPtr.Zero;
            IntPtr restrictedToken = IntPtr.Zero;
            ProcessInformation processInfo = new ProcessInformation();
            IntPtr safeProcess = IntPtr.Zero;
            bool creatorProcessOpen = false;
            bool creatorThreadOpen = false;
            string oldBarrier = Environment.GetEnvironmentVariable(BarrierEnvironment);
            string oldNonce = Environment.GetEnvironmentVariable(NonceEnvironment);
            try
            {
                processDescriptor = SecurityDescriptor(
                    currentUserSid,
                    DANGEROUS_PROCESS_ACCESS,
                    SAFE_PARENT_ACCESS,
                    "process");
                threadDescriptor = SecurityDescriptor(
                    currentUserSid,
                    DANGEROUS_THREAD_ACCESS,
                    SAFE_THREAD_ACCESS,
                    "thread");
                restrictedToken = CreateRestrictedPrimaryToken(currentUserSid);
                SecurityAttributes processAttributes = new SecurityAttributes
                {
                    nLength = Marshal.SizeOf(typeof(SecurityAttributes)),
                    lpSecurityDescriptor = processDescriptor,
                    bInheritHandle = false
                };
                SecurityAttributes threadAttributes = new SecurityAttributes
                {
                    nLength = Marshal.SizeOf(typeof(SecurityAttributes)),
                    lpSecurityDescriptor = threadDescriptor,
                    bInheritHandle = false
                };
                StartupInfo startup = new StartupInfo();
                startup.cb = Marshal.SizeOf(typeof(StartupInfo));

                Environment.SetEnvironmentVariable(
                    BarrierEnvironment,
                    barrierName,
                    EnvironmentVariableTarget.Process);
                Environment.SetEnvironmentVariable(
                    NonceEnvironment,
                    nonce,
                    EnvironmentVariableTarget.Process);

                StringBuilder commandLine = new StringBuilder(
                    BuildCommandLine(
                        pythonExecutable,
                        launchBoundary,
                        binder,
                        binderArguments));
                if (!CreateProcessAsUserW(
                    restrictedToken,
                    pythonExecutable,
                    commandLine,
                    ref processAttributes,
                    ref threadAttributes,
                    false,
                    0,
                    IntPtr.Zero,
                    workingDirectory,
                    ref startup,
                    out processInfo))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "birth-protected PyInstaller process creation failed");
                }
                creatorProcessOpen = true;
                creatorThreadOpen = true;

                RequireFreshProcessAccessDenied(processInfo.dwProcessId);
                RequireFreshThreadAccessDenied(processInfo.dwThreadId);

                if (String.Equals(
                    Environment.GetEnvironmentVariable(TestSiblingProbeEnvironment),
                    "1",
                    StringComparison.Ordinal))
                {
                    RunHostileSiblingProbe(pythonExecutable, processInfo.dwProcessId);
                }

                safeProcess = OpenProcess(
                    SAFE_PARENT_ACCESS,
                    false,
                    processInfo.dwProcessId);
                if (safeProcess == IntPtr.Zero)
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "unable to acquire safe protected-worker wait handle");
                }

                if (!CloseHandle(processInfo.hThread))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "unable to close protected-worker creator thread handle");
                }
                creatorThreadOpen = false;
                if (!CloseHandle(processInfo.hProcess))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "unable to close protected-worker creator process handle");
                }
                creatorProcessOpen = false;

                if (!SetEvent(barrier))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "unable to release protected-worker launch barrier");
                }

                if (WaitForSingleObject(safeProcess, INFINITE) != WAIT_OBJECT_0)
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "protected-worker wait failed");
                }
                uint exitCode;
                if (!GetExitCodeProcess(safeProcess, out exitCode))
                {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "protected-worker exit code is unavailable");
                }
                return unchecked((int)exitCode);
            }
            catch
            {
                if (creatorProcessOpen && processInfo.hProcess != IntPtr.Zero)
                {
                    TerminateProcess(processInfo.hProcess, 201);
                }
                throw;
            }
            finally
            {
                Environment.SetEnvironmentVariable(
                    BarrierEnvironment,
                    oldBarrier,
                    EnvironmentVariableTarget.Process);
                Environment.SetEnvironmentVariable(
                    NonceEnvironment,
                    oldNonce,
                    EnvironmentVariableTarget.Process);
                if (creatorThreadOpen && processInfo.hThread != IntPtr.Zero)
                {
                    CloseHandle(processInfo.hThread);
                }
                if (creatorProcessOpen && processInfo.hProcess != IntPtr.Zero)
                {
                    CloseHandle(processInfo.hProcess);
                }
                if (safeProcess != IntPtr.Zero)
                {
                    CloseHandle(safeProcess);
                }
                if (restrictedToken != IntPtr.Zero)
                {
                    CloseHandle(restrictedToken);
                }
                if (threadDescriptor != IntPtr.Zero)
                {
                    LocalFree(threadDescriptor);
                }
                if (processDescriptor != IntPtr.Zero)
                {
                    LocalFree(processDescriptor);
                }
                CloseHandle(barrier);
            }
        }
    }
}
