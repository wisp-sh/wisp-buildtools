// Bootstrap only. Generate Mono's native-call tables from the fixed runtime BCL.
using System;
using System.IO;
using System.Linq;
using Microsoft.Build.Framework;
using Microsoft.WebAssembly.Build.Tasks;

internal sealed class BuildLog : IBuildEngine
{
    public bool ContinueOnError => false;
    public int LineNumberOfTaskNode => 0;
    public int ColumnNumberOfTaskNode => 0;
    public string ProjectFileOfTaskNode => "csharp-bootstrap";
    public void LogErrorEvent(BuildErrorEventArgs e) => Console.Error.WriteLine(e.Message);
    public void LogWarningEvent(BuildWarningEventArgs e) => Console.Error.WriteLine(e.Message);
    public void LogMessageEvent(BuildMessageEventArgs e) { }
    public void LogCustomEvent(CustomBuildEventArgs e) { }
    public bool BuildProjectFile(string p, string[] t, System.Collections.IDictionary g,
        System.Collections.IDictionary o) => throw new NotSupportedException();
}
internal static class Generate
{
    public static int Main(string[] args)
    {
        var task = new ManagedToNativeGenerator {
            BuildEngine = new BuildLog(),
            Assemblies = Directory.GetFiles(args[0], "*.dll").OrderBy(p => p).ToArray(),
            PInvokeModules = new[] {"libSystem.Native", "libSystem.IO.Compression.Native"},
            PInvokeOutputPath = Path.Combine(args[1], "pinvoke-table.h"),
            InterpToNativeOutputPath = Path.Combine(args[1], "wasm_m2n_invoke.g.h")
        };
        return task.Execute() ? 0 : 1;
    }
}
