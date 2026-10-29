using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

// Semantic discovery runs in the same Wasm-hosted Roslyn as application emission.
// Reject a partial public API instead of silently dropping unsupported methods.
internal sealed class Discovery
{
    private readonly SortedDictionary<string, string> declarations = new(StringComparer.Ordinal);
    private readonly Dictionary<string, ITypeSymbol> owners = new(StringComparer.Ordinal);
    private readonly HashSet<ITypeSymbol> active = new(SymbolEqualityComparer.Default);
    private readonly HashSet<string> functions = new(StringComparer.Ordinal);
    private int expansion;

    private static string Name(string name)
    {
        var result = new StringBuilder();
        for (int i = 0; i < name.Length; i++) {
            char c = name[i];
            if (c == '_') { if (result.Length != 0 && result[result.Length - 1] != '-') result.Append('-'); continue; }
            if (!(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'))
                throw new ArgumentException("public API names require ASCII letters/digits: " + name);
            if (c >= 'A' && c <= 'Z' && result.Length != 0 && result[result.Length - 1] != '-' &&
                (char.IsLower(name[i - 1]) || char.IsDigit(name[i - 1]) || i + 1 < name.Length && char.IsLower(name[i + 1]))) result.Append('-');
            result.Append(char.ToLowerInvariant(c));
        }
        string value = result.ToString().TrimEnd('-');
        if (value.Length == 0 || value[0] < 'a' || value[0] > 'z') throw new ArgumentException("invalid public API name: " + name);
        return value;
    }
    private static bool Wisp(INamedTypeSymbol type, string name, int arity) =>
        type.Name == name && type.Arity == arity && type.ContainingNamespace.ToDisplayString() == "Wisp";

    private string Type(ITypeSymbol type, int depth = 0, bool nullable = true)
    {
        if (++expansion > 4096 || depth > 32) throw new ArgumentException("public API type expansion limit");
        if (nullable && type.IsReferenceType && type.NullableAnnotation == NullableAnnotation.Annotated)
            return "option<" + Type(type, depth + 1, false) + ">";
        string scalar = type.SpecialType switch {
            SpecialType.System_Boolean => "bool", SpecialType.System_Byte => "u8", SpecialType.System_SByte => "s8",
            SpecialType.System_UInt16 => "u16", SpecialType.System_Int16 => "s16", SpecialType.System_UInt32 => "u32", SpecialType.System_Int32 => "s32",
            SpecialType.System_UInt64 => "u64", SpecialType.System_Int64 => "s64", SpecialType.System_Single => "f32", SpecialType.System_Double => "f64", SpecialType.System_String => "string", _ => null
        };
        if (scalar != null) return scalar;
        if (type.ToDisplayString() == "System.Text.Rune") return "char";
        if (type is IArrayTypeSymbol array && array.Rank == 1) return "list<" + Type(array.ElementType, depth + 1) + ">";
        if (type is not INamedTypeSymbol named) throw new ArgumentException("unsupported public type: " + type);
        if (named.OriginalDefinition.SpecialType == SpecialType.System_Nullable_T || Wisp(named, "Option", 1))
            return "option<" + Type(named.TypeArguments[0], depth + 1) + ">";
        if (Wisp(named, "Result", 2)) {
            bool Unit(ITypeSymbol t) => t is INamedTypeSymbol n && Wisp(n, "Unit", 0);
            string ok = Unit(named.TypeArguments[0]) ? null : Type(named.TypeArguments[0], depth + 1);
            string err = Unit(named.TypeArguments[1]) ? null : Type(named.TypeArguments[1], depth + 1);
            return err != null ? "result<" + (ok ?? "_") + ", " + err + ">" : ok != null ? "result<" + ok + ">" : "result";
        }
        if (named.Arity == 1 && named.ContainingNamespace.ToDisplayString() == "System.Collections.Generic" &&
            new[] { "List", "IList", "ICollection", "IEnumerable", "IReadOnlyList", "IReadOnlyCollection" }.Contains(named.Name))
            return "list<" + Type(named.TypeArguments[0], depth + 1) + ">";
        if (named.IsTupleType && named.TupleElements.Length <= 7)
            return "tuple<" + string.Join(", ", named.TupleElements.Select(f => Type(f.Type, depth + 1))) + ">";
        if (named.Arity != 0 || named.SpecialType != SpecialType.None || named.DeclaredAccessibility != Accessibility.Public ||
            named.TypeKind != TypeKind.Enum && named.TypeKind != TypeKind.Class && named.TypeKind != TypeKind.Struct ||
            named.ContainingNamespace.ToDisplayString().StartsWith("System", StringComparison.Ordinal))
            throw new ArgumentException("unsupported public type: " + type + "; use a supported data record or an authored contract");
        string key = Name(named.Name);
        if (functions.Contains(key)) key += named.TypeKind == TypeKind.Enum ? "-enum" : "-record";
        if (owners.TryGetValue(key, out var owner) && !SymbolEqualityComparer.Default.Equals(owner, named))
            throw new ArgumentException("public type names collide after normalization: " + key);
        owners[key] = named;
        if (active.Contains(named)) throw new ArgumentException("recursive public data types are unsupported: " + named);
        if (declarations.ContainsKey(key)) return "%" + key;
        active.Add(named);
        var names = new HashSet<string>(StringComparer.Ordinal);
        string MemberName(string name) { string value = Name(name); if (!names.Add(value)) throw new ArgumentException("public member names collide: " + value); return "%" + value; }
        string declaration;
        if (named.TypeKind == TypeKind.Enum) {
            bool flags = named.GetAttributes().Any(a => a.AttributeClass?.ToDisplayString() == "System.FlagsAttribute");
            var fields = named.GetMembers().OfType<IFieldSymbol>().Where(f => f.HasConstantValue).ToArray();
            if (fields.Length == 0 || fields.Length > (flags ? 64 : 256)) throw new ArgumentException("public enum/flags size limit");
            var values = new HashSet<ulong>();
            foreach (var field in fields) {
                ulong bits = Bindings.EnumBits(field, named);
                if (!values.Add(bits) || flags && (bits == 0 || (bits & (bits - 1)) != 0))
                    throw new ArgumentException("public enum aliases and zero/composite flags need an authored contract: " + field);
            }
            declaration = (flags ? "flags" : "enum") + " %" + key + " { " + string.Join(", ", fields.Select(f => MemberName(f.Name))) + " }\n";
        } else {
            var members = Bindings.Members(named).Where(m => m is IFieldSymbol || m is IPropertySymbol p && p.GetMethod?.DeclaredAccessibility == Accessibility.Public).ToArray();
            if (members.Length == 0 || members.Length > 64) throw new ArgumentException("public records require 1 to 64 fields: " + type);
            declaration = "record %" + key + " {\n" + string.Concat(members.Select(m => "  " + MemberName(m.Name) + ": " + Type(Bindings.MemberType(m), depth + 1) + ",\n")) + "}\n";
        }
        active.Remove(named); declarations.Add(key, declaration);
        return "%" + key;
    }

    public static string Generate(CSharpCompilation compilation, JsonElement request)
    {
        string entry = request.TryGetProperty("entry", out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
        var types = Bindings.PublicTypes(compilation.Assembly.GlobalNamespace).Where(t => entry == null || t.ToDisplayString() == entry).ToArray();
        if (entry != null && types.Length != 1) throw new ArgumentException("entry must name one public non-generic source type: " + entry);
        var methods = types.SelectMany(t => t.GetMembers().OfType<IMethodSymbol>()).Where(m => m.IsStatic && !m.IsImplicitlyDeclared &&
            m.DeclaredAccessibility == Accessibility.Public && m.MethodKind == MethodKind.Ordinary).OrderBy(m => m.ToDisplayString(), StringComparer.Ordinal).ToArray();
        if (methods.Length == 0 || methods.Length > 256) throw new ArgumentException("discovery requires 1 to 256 public static methods");
        var discovery = new Discovery();
        foreach (var method in methods) {
            if (!discovery.functions.Add(Name(method.Name))) throw new ArgumentException("public method names collide; overloads require an authored contract: " + method.Name);
            if (method.Arity != 0 || method.ReturnsByRef || method.ReturnsByRefReadonly || method.Parameters.Any(p => p.RefKind != RefKind.None))
                throw new ArgumentException("generic and by-reference public methods require an authored contract: " + method);
        }
        var signatures = new StringBuilder();
        foreach (var method in methods) {
            var names = new HashSet<string>(StringComparer.Ordinal);
            string Parameter(IParameterSymbol p) { string name = Name(p.Name); if (!names.Add(name)) throw new ArgumentException("public parameter names collide: " + method); return "%" + name + ": " + discovery.Type(p.Type); }
            signatures.Append('%').Append(Name(method.Name)).Append(": func(").Append(string.Join(", ", method.Parameters.Select(Parameter))).Append(')');
            if (!method.ReturnsVoid) signatures.Append(" -> ").Append(discovery.Type(method.ReturnType));
            signatures.Append(";\n");
        }
        if (discovery.declarations.Keys.Any(discovery.functions.Contains)) throw new ArgumentException("public type and method names collide after normalization");
        return string.Concat(discovery.declarations.Values) + signatures;
    }
}
