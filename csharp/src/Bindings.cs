using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Wisp;

internal sealed class ExportBinding
{
    public string Wire = "", Key = "", Name = "";
    public int[] Parameters = Array.Empty<int>();
    public int Result = -1;
    public IMethodSymbol Method;
}
internal sealed class Bindings
{
    public readonly List<AbiType> Types = new List<AbiType>();
    public readonly List<ExportBinding> Exports = new List<ExportBinding>();
    private readonly JsonElement document;
    private readonly Dictionary<int, int> named = new Dictionary<int, int>();
    private readonly Dictionary<string, int> primitives = new Dictionary<string, int>();
    public static string Quote(string value) => JsonSerializer.Serialize(value);
    public Bindings(JsonElement request)
    {
        document = request.GetProperty("wit");
        var worldName = request.GetProperty("world").GetString();
        var worlds = document.GetProperty("worlds").EnumerateArray().Where(w => w.GetProperty("name").GetString() == worldName).ToArray();
        if (worlds.Length != 1) throw new ArgumentException("select exactly one authored WIT world");
        var world = worlds[0];
        if (world.GetProperty("imports").EnumerateObject().Any()) throw new ArgumentException("C# WIT imports are not supported yet");
        foreach (var item in world.GetProperty("exports").EnumerateObject()) {
            if (item.Value.TryGetProperty("function", out var function)) AddFunction(function, "", "");
            else if (item.Value.TryGetProperty("interface", out var iface)) {
                int id = iface.GetProperty("id").GetInt32(); var definition = document.GetProperty("interfaces")[id];
                string name = definition.GetProperty("name").GetString();
                string prefix = item.Name;
                if (prefix == "interface-" + id.ToString(CultureInfo.InvariantCulture)) {
                    string package = document.GetProperty("packages")[definition.GetProperty("package").GetInt32()].GetProperty("name").GetString();
                    int at = package.IndexOf('@'); prefix = at < 0 ? package + "/" + name : package.Substring(0, at) + "/" + name + package.Substring(at);
                }
                foreach (var member in definition.GetProperty("functions").EnumerateObject()) AddFunction(member.Value, prefix, name ?? prefix);
            } else throw new ArgumentException("unsupported WIT world export");
        }
        if (Exports.Count == 0 || Exports.Count > 256) throw new ArgumentException("C# requires between 1 and 256 WIT function exports");
        if (Exports.Any(e => e.Wire == "wisp-validate")) throw new ArgumentException("wisp-validate is reserved for compiler validation");
        Canonical.Layout(Types.ToArray());
    }
    private void AddFunction(JsonElement function, string prefix, string shortName)
    {
        if (function.GetProperty("kind").GetString() != "freestanding") throw new ArgumentException("resource methods are unsupported");
        string name = function.GetProperty("name").GetString();
        Exports.Add(new ExportBinding {
            Name = name, Wire = prefix.Length == 0 ? name : prefix + "#" + name,
            Key = prefix.Length == 0 ? name : shortName + "." + name,
            Parameters = function.GetProperty("params").EnumerateArray().Select(p => Resolve(p.GetProperty("type"))).ToArray(),
            Result = !function.TryGetProperty("result", out var result) || result.ValueKind == JsonValueKind.Null ? -1 : Resolve(result)
        });
    }
    private int Primitive(string kind)
    {
        if (primitives.TryGetValue(kind, out int id)) return id;
        id = Types.Count; Types.Add(new AbiType { Kind = kind }); primitives.Add(kind, id); return id;
    }
    private int Resolve(JsonElement reference)
    {
        if (reference.ValueKind == JsonValueKind.Null) return Primitive("unit");
        if (reference.ValueKind == JsonValueKind.String) return Primitive(reference.GetString());
        int key = reference.GetInt32();
        if (named.TryGetValue(key, out int id)) return id;
        id = Types.Count; var type = new AbiType(); Types.Add(type); named.Add(key, id);
        var kind = document.GetProperty("types")[key].GetProperty("kind");
        if (kind.ValueKind == JsonValueKind.String) { type.Kind = "type"; type.Children = new[] { Primitive(kind.GetString()) }; return id; }
        var pair = kind.EnumerateObject().Single(); type.Kind = pair.Name;
        switch (pair.Name) {
            case "type": type.Children = new[] { Resolve(pair.Value) }; break;
            case "list": type.Children = new[] { Resolve(pair.Value) }; break;
            case "option": type.Children = new[] { Primitive("unit"), Resolve(pair.Value) }; break;
            case "result": type.Children = new[] { pair.Value.TryGetProperty("ok", out var ok) ? Resolve(ok) : Primitive("unit"), pair.Value.TryGetProperty("err", out var err) ? Resolve(err) : Primitive("unit") }; break;
            case "record":
                var fields = pair.Value.GetProperty("fields").EnumerateArray().ToArray();
                type.Names = fields.Select(f => f.GetProperty("name").GetString()).ToArray(); type.Children = fields.Select(f => Resolve(f.GetProperty("type"))).ToArray(); break;
            case "tuple":
                type.Children = pair.Value.GetProperty("types").EnumerateArray().Select(Resolve).ToArray();
                type.Names = Enumerable.Range(1, type.Children.Length).Select(i => "item" + i.ToString(CultureInfo.InvariantCulture)).ToArray(); break;
            case "enum": type.Names = pair.Value.GetProperty("cases").EnumerateArray().Select(c => c.GetProperty("name").GetString()).ToArray(); break;
            case "flags": type.Names = pair.Value.GetProperty("flags").EnumerateArray().Select(c => c.GetProperty("name").GetString()).ToArray(); break;
            default: throw new ArgumentException("unsupported WIT type: " + pair.Name);
        }
        return id;
    }
    private static IEnumerable<INamedTypeSymbol> PublicTypes(INamespaceOrTypeSymbol parent)
    {
        foreach (var member in parent.GetMembers()) {
            if (member is INamespaceSymbol ns) foreach (var type in PublicTypes(ns)) yield return type;
            else if (member is INamedTypeSymbol type && type.DeclaredAccessibility == Accessibility.Public && type.Arity == 0) {
                yield return type;
                foreach (var nested in PublicTypes(type)) yield return nested;
            }
        }
    }
    private static IEnumerable<ISymbol> Members(INamedTypeSymbol type)
    {
        for (var current = type; current != null; current = current.BaseType)
            foreach (var m in current.GetMembers()) if (!m.IsStatic && m.DeclaredAccessibility == Accessibility.Public && (m is IPropertySymbol p && !p.IsIndexer || m is IFieldSymbol)) yield return m;
    }
    private static ITypeSymbol MemberType(ISymbol symbol) => symbol is IPropertySymbol p ? p.Type : ((IFieldSymbol)symbol).Type;
    private static bool Writable(ISymbol symbol) => symbol is IPropertySymbol p ? p.SetMethod?.DeclaredAccessibility == Accessibility.Public : !((IFieldSymbol)symbol).IsReadOnly;
    private static bool IsWisp(INamedTypeSymbol type, string name, int arity) => type.Name == name && type.Arity == arity && type.ContainingNamespace.ToDisplayString() == "Wisp";
    private static ulong EnumBits(IFieldSymbol field, INamedTypeSymbol type)
    {
        ulong bits; try { bits = Convert.ToUInt64(field.ConstantValue); } catch (OverflowException) { bits = unchecked((ulong)Convert.ToInt64(field.ConstantValue)); }
        int width = type.EnumUnderlyingType.SpecialType == SpecialType.System_Byte || type.EnumUnderlyingType.SpecialType == SpecialType.System_SByte ? 8 :
            type.EnumUnderlyingType.SpecialType == SpecialType.System_Int16 || type.EnumUnderlyingType.SpecialType == SpecialType.System_UInt16 ? 16 :
            type.EnumUnderlyingType.SpecialType == SpecialType.System_Int32 || type.EnumUnderlyingType.SpecialType == SpecialType.System_UInt32 ? 32 : 64;
        return width == 64 ? bits : bits & ((1UL << width) - 1);
    }
    private bool Compatible(int id, ITypeSymbol actual, bool input, int depth = 0)
    {
        if (depth > 32) return false;
        var t = Types[id]; var namedType = actual as INamedTypeSymbol;
        if (t.Kind == "type") return Compatible(t.Children[0], actual, input, depth + 1);
        string scalar = actual.SpecialType switch {
            SpecialType.System_Boolean => "bool", SpecialType.System_Byte => "u8", SpecialType.System_SByte => "s8",
            SpecialType.System_UInt16 => "u16", SpecialType.System_Int16 => "s16", SpecialType.System_UInt32 => "u32", SpecialType.System_Int32 => "s32",
            SpecialType.System_UInt64 => "u64", SpecialType.System_Int64 => "s64", SpecialType.System_Single => "f32", SpecialType.System_Double => "f64", SpecialType.System_String => "string", _ => ""
        };
        if (scalar.Length != 0 && t.Kind != "option") return scalar == t.Kind;
        if (t.Kind == "unit") return namedType != null && IsWisp(namedType, "Unit", 0);
        if (t.Kind == "char") return actual.ToDisplayString() == "System.Text.Rune";
        if (t.Kind == "list") {
            ITypeSymbol element;
            if (actual is IArrayTypeSymbol array && array.Rank == 1) element = array.ElementType;
            else if (namedType != null && namedType.Arity == 1 && namedType.ContainingNamespace.ToDisplayString() == "System.Collections.Generic" &&
                new[] { "List", "IList", "ICollection", "IEnumerable", "IReadOnlyList", "IReadOnlyCollection" }.Contains(namedType.Name)) element = namedType.TypeArguments[0];
            else return false;
            return Compatible(t.Children[0], element, input, depth + 1);
        }
        if (t.Kind == "option") {
            var child = namedType != null && (IsWisp(namedType, "Option", 1) || namedType.OriginalDefinition.SpecialType == SpecialType.System_Nullable_T) ? namedType.TypeArguments[0] : !actual.IsValueType ? actual : null;
            return child != null && Compatible(t.Children[1], child, input, depth + 1);
        }
        if (t.Kind == "result") return namedType != null && IsWisp(namedType, "Result", 2) && Compatible(t.Children[0], namedType.TypeArguments[0], input, depth + 1) && Compatible(t.Children[1], namedType.TypeArguments[1], input, depth + 1);
        if (t.Kind == "enum" || t.Kind == "flags") {
            if (namedType?.TypeKind != TypeKind.Enum) return false;
            var constants = new List<ulong>();
            foreach (string name in t.Names) {
                var fields = namedType.GetMembers().OfType<IFieldSymbol>().Where(f => f.HasConstantValue && Canonical.NameKey(f.Name) == Canonical.NameKey(name)).ToArray();
                if (fields.Length != 1) return false;
                ulong value = EnumBits(fields[0], namedType);
                if (constants.Contains(value) || t.Kind == "flags" && (value == 0 || (value & (value - 1)) != 0)) return false;
                constants.Add(value);
            }
            return true;
        }
        if (t.Kind == "tuple") return namedType != null && namedType.IsTupleType && t.Children.Length <= 7 && namedType.TupleElements.Length == t.Children.Length &&
            t.Children.Select((child, i) => Compatible(child, namedType.TupleElements[i].Type, input, depth + 1)).All(v => v);
        if (t.Kind == "record") {
            if (namedType == null || namedType.TypeKind != TypeKind.Class && namedType.TypeKind != TypeKind.Struct && !(!input && namedType.TypeKind == TypeKind.Interface)) return false;
            var fields = new List<ISymbol>();
            foreach (var pair in t.Names.Select((name, i) => (name, i))) {
                var matches = Members(namedType).Where(m => Canonical.NameKey(m.Name) == Canonical.NameKey(pair.name)).ToArray();
                if (matches.Length != 1 || matches[0] is IPropertySymbol p && p.GetMethod?.DeclaredAccessibility != Accessibility.Public || !Compatible(t.Children[pair.i], MemberType(matches[0]), input, depth + 1)) return false;
                fields.Add(matches[0]);
            }
            if (!input) return true;
            if (namedType.InstanceConstructors.Any(c => c.DeclaredAccessibility == Accessibility.Public && c.Parameters.Length == fields.Count && c.Parameters.All(p => {
                int index = fields.FindIndex(m => Canonical.NameKey(m.Name) == Canonical.NameKey(p.Name));
                return index >= 0 && SymbolEqualityComparer.Default.Equals(p.Type, MemberType(fields[index]));
            }))) return true;
            return (namedType.IsValueType || namedType.InstanceConstructors.Any(c => c.DeclaredAccessibility == Accessibility.Public && c.Parameters.Length == 0)) && fields.All(Writable);
        }
        return false;
    }
    public string Generate(CSharpCompilation compilation, JsonElement request)
    {
        string entry = request.TryGetProperty("entry", out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
        var mappings = request.GetProperty("exports").EnumerateObject().ToDictionary(p => p.Name, p => p.Value.GetString());
        var allTypes = PublicTypes(compilation.Assembly.GlobalNamespace).ToArray();
        var methods = allTypes.SelectMany(t => t.GetMembers().OfType<IMethodSymbol>()).Where(m => m.DeclaredAccessibility == Accessibility.Public && m.IsStatic && m.Arity == 0 && !m.ReturnsByRef && !m.ReturnsByRefReadonly && m.MethodKind == MethodKind.Ordinary && m.Parameters.All(p => p.RefKind == RefKind.None)).ToArray();
        var code = new StringBuilder("namespace Wisp.Generated { public static class Bridge { static Bridge() { global::Wisp.Canonical.Configure(new global::Wisp.AbiType[] {\n");
        foreach (var t in Types) code.Append("new global::Wisp.AbiType { Kind = ").Append(Quote(t.Kind)).Append(", Children = new int[] {").Append(string.Join(",", t.Children)).Append("}, Names = new string[] {").Append(string.Join(",", t.Names.Select(Quote))).Append("} },\n");
        code.Append("}); } public static void Invoke(int index, int input, int mode, int output) { switch(index) {\n");
        for (int i = 0; i < Exports.Count; i++) {
            var export = Exports[i]; string mapping = null;
            foreach (var key in new[] { export.Wire, export.Key }) if (mappings.TryGetValue(key, out mapping)) break;
            var candidates = methods.Where(m => mapping != null ? m.ContainingType.ToDisplayString() + "." + m.Name == mapping :
                Canonical.NameKey(m.Name) == Canonical.NameKey(export.Name) && (entry == null || m.ContainingType.ToDisplayString() == entry)).Where(m =>
                    m.Parameters.Length == export.Parameters.Length && m.Parameters.Select((p, j) => Compatible(export.Parameters[j], p.Type, true)).All(v => v) &&
                    (export.Result < 0 ? m.ReturnsVoid : !m.ReturnsVoid && Compatible(export.Result, m.ReturnType, false))).ToArray();
            if (candidates.Length != 1) throw new ArgumentException($"WIT export {export.Key} must match exactly one public static non-generic C# method with compatible types; found {candidates.Length}. Use --entry Namespace.Type or --export {export.Key}=Namespace.Type.Method to disambiguate.");
            export.Method = candidates[0]; var method = export.Method;
            code.Append("case ").Append(i).Append(": {\n");
            if (method.Parameters.Length != 0) code.Append("int position = 0;\n");
            var args = new List<string>();
            for (int j = 0; j < method.Parameters.Length; j++) {
                string type = method.Parameters[j].Type.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat);
                code.Append("var arg").Append(j).Append(" = (").Append(type).Append(")global::Wisp.Canonical.Parameter(").Append(export.Parameters[j]).Append(", typeof(").Append(type).Append("), input, mode, ref position);\n"); args.Add("arg" + j);
            }
            string invocation = method.ContainingType.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat) + ".@" + method.Name + "(" + string.Join(",", args) + ")";
            if (export.Result < 0) code.Append(invocation).Append(";\n");
            else { string type = method.ReturnType.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat); code.Append("var result = ").Append(invocation).Append("; global::Wisp.Canonical.Result(").Append(export.Result).Append(", typeof(").Append(type).Append("), result, output);\n"); }
            code.Append("return; }\n");
        }
        foreach (string key in mappings.Keys) if (!Exports.Any(e => e.Wire == key || e.Key == key)) throw new ArgumentException("export mapping does not name a selected WIT export: " + key);
        return code.Append("default: throw new global::System.ArgumentException(\"invalid export index\"); } } } }").ToString();
    }
}
