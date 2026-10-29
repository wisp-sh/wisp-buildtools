using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

internal static class Compiler
{
    private static string AssemblyName(byte[] bytes, out string[] dependencies)
    {
        using var pe = new PEReader(new MemoryStream(bytes));
        var header = pe.PEHeaders.CorHeader;
        if (!pe.HasMetadata || header == null || (header.Flags & CorFlags.ILOnly) == 0 ||
            (header.Flags & CorFlags.NativeEntryPoint) != 0 || header.ManagedNativeHeaderDirectory.Size != 0)
            throw new ArgumentException("dependency must be a pure managed IL assembly, without native or ReadyToRun code");
        var reader = pe.GetMetadataReader();
        if (!reader.IsAssembly) throw new ArgumentException("dependency must be an assembly");
        dependencies = reader.AssemblyReferences.Select(h => reader.GetString(reader.GetAssemblyReference(h).Name)).ToArray();
        return reader.GetString(reader.GetAssemblyDefinition().Name);
    }
    private static void ValidName(string name)
    {
        if (string.IsNullOrEmpty(name) || name.Length > 240 || !name.All(c => c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-') || name == "." || name == "..")
            throw new ArgumentException("invalid managed assembly name: " + name);
    }
    private static bool Errors(CSharpCompilation compilation)
    {
        var errors = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
        foreach (var error in errors) Console.Error.WriteLine(error);
        return errors.Length != 0;
    }
    public static int Main(string[] args)
    {
        try {
            if (args.Length != 1) throw new ArgumentException("expected compiler request JSON");
            using var json = JsonDocument.Parse(File.ReadAllText(args[0])); var request = json.RootElement;
            string name = request.GetProperty("assembly").GetString(); ValidName(name);
            var runtime = request.GetProperty("runtimeAssemblies").EnumerateArray().Select(e => e.GetString()).ToHashSet(StringComparer.OrdinalIgnoreCase);
            if (runtime.Contains(name)) throw new ArgumentException("application assembly name conflicts with the runtime");
            JsonElement options = request.TryGetProperty("options", out var configured) ? configured : default;
            string Setting(string key, string fallback) => options.ValueKind == JsonValueKind.Object && options.TryGetProperty(key, out var value) ? value.GetString() : fallback;
            bool Enabled(string key) => options.ValueKind == JsonValueKind.Object && options.TryGetProperty(key, out var value) && value.GetBoolean();
            string language = Setting("language", "12.0");
            if (!LanguageVersionFacts.TryParse(language, out var languageVersion) || languageVersion < LanguageVersion.CSharp8 || languageVersion > LanguageVersion.CSharp12 || languageVersion == LanguageVersion.Preview)
                throw new ArgumentException("supported language versions are C# 8 through 12; preview/latest syntax is not pinned");
            var defines = new[] { "NET", "NET8_0", "NETCOREAPP", "NETCOREAPP1_0_OR_GREATER", "NETCOREAPP1_1_OR_GREATER", "NETCOREAPP2_0_OR_GREATER", "NETCOREAPP2_1_OR_GREATER", "NETCOREAPP2_2_OR_GREATER", "NETCOREAPP3_0_OR_GREATER", "NETCOREAPP3_1_OR_GREATER", "NET5_0_OR_GREATER", "NET6_0_OR_GREATER", "NET7_0_OR_GREATER", "NET8_0_OR_GREATER", "WISP", "WASI" }.Concat(
                options.ValueKind == JsonValueKind.Object && options.TryGetProperty("defines", out var symbols) ? symbols.EnumerateArray().Select(e => e.GetString()) : Array.Empty<string>());
            var parse = new CSharpParseOptions(languageVersion, preprocessorSymbols: defines);
            var sources = request.GetProperty("sources").EnumerateArray().Select(s => CSharpSyntaxTree.ParseText(
                File.ReadAllText(s.GetProperty("path").GetString()), parse, path: s.GetProperty("name").GetString())).ToList();
            if (sources.Count == 0 || sources.Count > 256) throw new ArgumentException("expected between 1 and 256 C# source files");
            if (Enabled("implicitUsings")) sources.Add(CSharpSyntaxTree.ParseText("global using System; global using System.Collections.Generic; global using System.IO; global using System.Linq; global using System.Net.Http; global using System.Threading; global using System.Threading.Tasks;", parse, path: "wisp.usings.g.cs"));
            var nullable = Setting("nullable", "enable").ToLowerInvariant() switch {
                "enable" => NullableContextOptions.Enable, "disable" => NullableContextOptions.Disable,
                "annotations" => NullableContextOptions.Annotations, "warnings" => NullableContextOptions.Warnings,
                _ => throw new ArgumentException("unsupported Nullable setting")
            };
            var references = Directory.GetFiles(request.GetProperty("framework").GetString(), "*.dll").OrderBy(p => p, StringComparer.Ordinal)
                .Select(p => MetadataReference.CreateFromImage(File.ReadAllBytes(p))).ToList();
            var dependencies = new SortedDictionary<string, byte[]>(StringComparer.Ordinal);
            var required = new Dictionary<string, string[]>();
            foreach (var file in request.GetProperty("references").EnumerateArray()) {
                byte[] bytes = File.ReadAllBytes(file.GetString());
                if (bytes.Length > 16 * 1024 * 1024) throw new ArgumentException("managed reference exceeds 16 MiB");
                string dependency = AssemblyName(bytes, out string[] needed); ValidName(dependency);
                if (runtime.Contains(dependency) || string.Equals(name, dependency, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("managed dependency shadows an application/runtime assembly: " + dependency);
                var prior = dependencies.FirstOrDefault(p => string.Equals(p.Key, dependency, StringComparison.OrdinalIgnoreCase));
                if (prior.Key != null) { if (!prior.Value.SequenceEqual(bytes)) throw new ArgumentException("conflicting assemblies named " + dependency); continue; }
                dependencies.Add(dependency, bytes); required.Add(dependency, needed); references.Add(MetadataReference.CreateFromImage(bytes));
            }
            if (dependencies.Count >= 128) throw new ArgumentException("managed bundle supports at most 128 assemblies");
            foreach (var dependency in required) foreach (string needed in dependency.Value)
                if (!runtime.Contains(needed) && !dependencies.Keys.Any(k => string.Equals(k, needed, StringComparison.OrdinalIgnoreCase)) && !string.Equals(name, needed, StringComparison.OrdinalIgnoreCase))
                    throw new ArgumentException($"managed dependency {dependency.Key} requires missing assembly {needed}; supply its implementation DLL with --reference");
            var compilation = CSharpCompilation.Create(name, sources, references,
                new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary, optimizationLevel: options.ValueKind == JsonValueKind.Object && options.TryGetProperty("optimize", out var optimize) && !optimize.GetBoolean() ? OptimizationLevel.Debug : OptimizationLevel.Release,
                    concurrentBuild: false, deterministic: true, nullableContextOptions: nullable, allowUnsafe: Enabled("unsafe"), checkOverflow: Enabled("checked"),
                    generalDiagnosticOption: Enabled("warningsAsErrors") ? ReportDiagnostic.Error : ReportDiagnostic.Default));
            if (Errors(compilation)) return 1;
            if (request.GetProperty("mode").GetString() == "discover") {
                string api = Discovery.Generate(compilation, request);
                File.WriteAllText(request.GetProperty("output").GetString(), api);
                File.WriteAllText(request.GetProperty("report").GetString(), JsonSerializer.Serialize(new { assembly = name, api }));
                return 0;
            }
            Bindings bindings = null;
            if (request.GetProperty("mode").GetString() == "component") {
                bindings = new Bindings(request);
                compilation = compilation.AddSyntaxTrees(CSharpSyntaxTree.ParseText(bindings.Generate(compilation, request), parse, path: "wisp.bindings.g.cs"));
                if (Errors(compilation)) return 1;
            }
            using var output = new MemoryStream(); var emitted = compilation.Emit(output);
            foreach (var diagnostic in emitted.Diagnostics) Console.Error.WriteLine(diagnostic);
            if (!emitted.Success) return 1;
            byte[] il = output.ToArray(); if (il.Length > 16 * 1024 * 1024) throw new ArgumentException("emitted IL exceeds 16 MiB");
            string destination = request.GetProperty("output").GetString();
            if (bindings == null) File.WriteAllBytes(destination, il);
            else {
                using var bundle = new MemoryStream(); using var writer = new BinaryWriter(bundle, Encoding.UTF8, true);
                writer.Write(dependencies.Count + 1);
                void Assembly(string assembly, byte[] bytes) { byte[] encoded = Encoding.ASCII.GetBytes(assembly); writer.Write(encoded.Length); writer.Write(encoded); writer.Write(bytes.Length); writer.Write(bytes); }
                Assembly(name, il); foreach (var dependency in dependencies) Assembly(dependency.Key, dependency.Value);
                byte[] packaged = WasmModule.Package(File.ReadAllBytes(request.GetProperty("template").GetString()), bundle.ToArray(), bindings);
                File.WriteAllBytes(destination, packaged);
            }
            if (request.TryGetProperty("report", out var report)) File.WriteAllText(report.GetString(), JsonSerializer.Serialize(new {
                assembly = name, ilBytes = il.Length, dependencies = dependencies.Select(p => new { name = p.Key, bytes = p.Value.Length }).ToArray(),
                exports = bindings == null ? Array.Empty<object>() : bindings.Exports.Select(e => (object)new { wit = e.Wire, method = e.Method.ToDisplayString() }).ToArray()
            }));
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1;
        }
    }
}
