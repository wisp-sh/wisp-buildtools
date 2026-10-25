using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Buffers.Binary;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

internal static class Compiler
{
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length != 4) throw new ArgumentException("expected SOURCE OUTPUT REFERENCES TEMPLATE");
            var tree = CSharpSyntaxTree.ParseText(File.ReadAllText(args[0]), path: Path.GetFileName(args[0]));
            var references = Directory.GetFiles(args[2], "*.dll").OrderBy(p => p)
                .Select(p => MetadataReference.CreateFromImage(File.ReadAllBytes(p)));
            var compilation = CSharpCompilation.Create("Application", new[] {tree}, references,
                new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary,
                    optimizationLevel: OptimizationLevel.Release, concurrentBuild: false,
                    deterministic: true));
            var errors = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
            if (errors.Length != 0)
            {
                foreach (var error in errors) Console.Error.WriteLine(error);
                return 1;
            }
            var type = compilation.GetTypeByMetadataName("Calculator");
            var methods = type?.GetMembers("Add").OfType<IMethodSymbol>().ToArray();
            if (type == null || type.DeclaredAccessibility != Accessibility.Public || type.Arity != 0 ||
                methods == null || methods.Length != 1 || !methods[0].IsStatic || methods[0].Arity != 0 ||
                methods[0].DeclaredAccessibility != Accessibility.Public || methods[0].ReturnsByRef ||
                methods[0].ReturnsByRefReadonly || methods[0].ReturnType.SpecialType != SpecialType.System_Int32 ||
                methods[0].Parameters.Length != 2 || methods[0].Parameters.Any(p =>
                    p.RefKind != RefKind.None || p.Type.SpecialType != SpecialType.System_Int32))
                throw new ArgumentException("expected public Calculator.Add(int, int) returning int, static and non-generic, without overloads");
            using var output = new MemoryStream();
            var result = compilation.Emit(output);
            foreach (var diagnostic in result.Diagnostics) Console.Error.WriteLine(diagnostic);
            if (!result.Success) return 1;
            var il = output.ToArray();
            const int capacity = 1024 * 1024;
            if (il.Length > capacity) throw new ArgumentException("emitted IL exceeds the 1 MiB payload limit");
            var template = File.ReadAllBytes(args[3]);
            var marker = Encoding.ASCII.GetBytes("WISP_CSHARP_IL_PAYLOAD_V1");
            int position = template.AsSpan().IndexOf(marker);
            if (position < 0 || template.AsSpan(position + marker.Length).IndexOf(marker) >= 0 ||
                template.Length - position < 36 + capacity)
                throw new ArgumentException("invalid runtime template payload slot");
            BinaryPrimitives.WriteInt32LittleEndian(template.AsSpan(position + 32, 4), il.Length);
            il.CopyTo(template, position + 36);
            File.WriteAllBytes(args[1], template);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
    }
}
