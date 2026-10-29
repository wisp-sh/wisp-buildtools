// Fixed runtime support. Application bindings and source compilation execute in Wasm.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;

namespace Wisp
{
    public readonly struct Unit { }
    public struct Option<T>
    {
        public bool HasValue;
        public T Value;
        public static Option<T> Some(T value) => new Option<T> { HasValue = true, Value = value };
        public static Option<T> None => default;
    }
    public struct Result<T, E>
    {
        public bool IsOk;
        public T Value;
        public E Error;
        public static Result<T, E> Ok(T value) => new Result<T, E> { IsOk = true, Value = value };
        public static Result<T, E> Err(E error) => new Result<T, E> { IsOk = false, Error = error };
    }
    public sealed class AbiType
    {
        public string Kind = "unit";
        public int[] Children = Array.Empty<int>();
        public string[] Names = Array.Empty<string>();
        public int Size, Align = 1, PayloadOffset;
        public int[] Offsets = Array.Empty<int>();
        public byte[] Flat = Array.Empty<byte>();
    }
    public static unsafe class Canonical
    {
        public const byte I32 = 0x7f, I64 = 0x7e, F32 = 0x7d, F64 = 0x7c;
        public const int MaxBytes = 32 * 1024 * 1024, MaxElements = 262144;
        private static AbiType[] types = Array.Empty<AbiType>();
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
        [MethodImpl(MethodImplOptions.InternalCall)]
        private static extern int Allocate(int bytes);
        [MethodImpl(MethodImplOptions.InternalCall)]
        private static extern void Release(int pointer);

        public static string NameKey(string name) => new string(name.Where(char.IsLetterOrDigit).Select(char.ToLowerInvariant).ToArray());
        public static int Aligned(int value, int align) => checked((value + align - 1) / align * align);
        public static void Configure(AbiType[] definitions) { Layout(definitions); types = definitions; }
        public static void Layout(AbiType[] definitions)
        {
            if (definitions.Length > 1024) throw new ArgumentException("WIT type count exceeds 1024");
            var states = new byte[definitions.Length];
            void Shape(int id, int depth)
            {
                if (depth > 32 || id < 0 || id >= definitions.Length) throw new ArgumentException("invalid or deeply nested WIT type");
                if (states[id] == 2) return;
                if (states[id] == 1) throw new ArgumentException("recursive WIT types are unsupported");
                states[id] = 1;
                var t = definitions[id];
                foreach (var child in t.Children) Shape(child, depth + 1);
                var children = t.Children.Select(i => definitions[i]).ToArray();
                switch (t.Kind)
                {
                    case "unit": break;
                    case "bool": case "u8": case "s8": t.Size = t.Align = 1; t.Flat = new[] { I32 }; break;
                    case "u16": case "s16": t.Size = t.Align = 2; t.Flat = new[] { I32 }; break;
                    case "u32": case "s32": case "char": t.Size = t.Align = 4; t.Flat = new[] { I32 }; break;
                    case "u64": case "s64": t.Size = t.Align = 8; t.Flat = new[] { I64 }; break;
                    case "f32": t.Size = t.Align = 4; t.Flat = new[] { F32 }; break;
                    case "f64": t.Size = t.Align = 8; t.Flat = new[] { F64 }; break;
                    case "string": case "list": t.Size = 8; t.Align = 4; t.Flat = new[] { I32, I32 }; break;
                    case "type": t.Size = children[0].Size; t.Align = children[0].Align; t.Flat = children[0].Flat; break;
                    case "record": case "tuple":
                        if (children.Length > 256) throw new ArgumentException("record/tuple field limit is 256");
                        t.Offsets = new int[children.Length];
                        for (int i = 0; i < children.Length; i++) {
                            t.Size = Aligned(t.Size, children[i].Align); t.Offsets[i] = t.Size;
                            t.Size = checked(t.Size + children[i].Size); t.Align = Math.Max(t.Align, children[i].Align);
                        }
                        t.Size = Aligned(t.Size, t.Align); t.Flat = children.SelectMany(c => c.Flat).ToArray(); break;
                    case "enum":
                        t.Size = t.Align = t.Names.Length <= 256 ? 1 : t.Names.Length <= 65536 ? 2 : 4;
                        t.Flat = new[] { I32 }; break;
                    case "flags":
                        if (t.Names.Length > 64) throw new ArgumentException("C# flags support at most 64 names");
                        t.Align = t.Names.Length <= 8 ? 1 : t.Names.Length <= 16 ? 2 : 4;
                        t.Size = t.Names.Length == 0 ? 0 : t.Names.Length <= 16 ? t.Align : 4 * ((t.Names.Length + 31) / 32);
                        t.Flat = Enumerable.Repeat(I32, (t.Names.Length + 31) / 32).ToArray(); break;
                    case "option": case "result":
                        t.Align = Math.Max(1, children.Max(c => c.Align)); t.PayloadOffset = Aligned(1, t.Align);
                        t.Size = Aligned(checked(t.PayloadOffset + children.Max(c => c.Size)), t.Align);
                        var joined = new byte[children.Max(c => c.Flat.Length)];
                        for (int i = 0; i < joined.Length; i++) {
                            byte a = children[0].Flat.ElementAtOrDefault(i), b = children[1].Flat.ElementAtOrDefault(i);
                            joined[i] = a == 0 ? b : b == 0 || a == b ? a : (a == I32 || a == F32) && (b == I32 || b == F32) ? I32 : I64;
                        }
                        t.Flat = new[] { I32 }.Concat(joined).ToArray(); break;
                    default: throw new ArgumentException("unsupported WIT type: " + t.Kind);
                }
                if (t.Size > MaxBytes || t.Flat.Length > 4096) throw new ArgumentException("WIT value layout exceeds limits");
                states[id] = 2;
            }
            for (int i = 0; i < definitions.Length; i++) Shape(i, 0);
        }
        private static AbiType TypeAt(int id) => types[id];
        private static uint U32(int ptr) => *(uint*)(uint)ptr;
        private static void Put32(int ptr, uint value) => *(uint*)(uint)ptr = value;
        private static int Plus(int ptr, int offset) => unchecked((int)((uint)ptr + (uint)offset));
        private static int Length(ulong value, int max) => value <= (ulong)max ? (int)value : throw new ArgumentException("canonical value exceeds size limit");
        private static ulong Bits(object value) {
            ulong bits; try { bits = Convert.ToUInt64(value); } catch (OverflowException) { bits = unchecked((ulong)Convert.ToInt64(value)); }
            if (!value.GetType().IsEnum) return bits;
            Type underlying = Enum.GetUnderlyingType(value.GetType());
            int width = underlying == typeof(byte) || underlying == typeof(sbyte) ? 8 : underlying == typeof(short) || underlying == typeof(ushort) ? 16 : underlying == typeof(int) || underlying == typeof(uint) ? 32 : 64;
            return width == 64 ? bits : bits & ((1UL << width) - 1);
        }
        private static MemberInfo Member(Type type, string name) => type.GetMembers(BindingFlags.Public | BindingFlags.Instance)
            .Single(m => NameKey(m.Name) == NameKey(name) && (m is FieldInfo || m is PropertyInfo p && p.GetIndexParameters().Length == 0));
        private static Type MemberType(MemberInfo m) => m is FieldInfo f ? f.FieldType : ((PropertyInfo)m).PropertyType;
        private static object Get(MemberInfo m, object value) => m is FieldInfo f ? f.GetValue(value) : ((PropertyInfo)m).GetValue(value);
        private static void Set(MemberInfo m, object value, object item) { if (m is FieldInfo f) f.SetValue(value, item); else ((PropertyInfo)m).SetValue(value, item); }
        private static string EnumName(Type type, string wire) => Enum.GetNames(type).Single(n => NameKey(n) == NameKey(wire));
        private static Type Element(Type type) => type.IsArray ? type.GetElementType() : type.GetGenericArguments()[0];
        private static bool IsOption(Type type) => type.IsGenericType && type.GetGenericTypeDefinition() == typeof(Option<>);

        private static void ReleaseFlat(int id, ulong[] raw, ref int at)
        {
            var t = TypeAt(id);
            if (t.Kind == "type") { ReleaseFlat(t.Children[0], raw, ref at); return; }
            if (t.Kind == "record" || t.Kind == "tuple") { foreach (int child in t.Children) ReleaseFlat(child, raw, ref at); return; }
            if (t.Kind == "option" || t.Kind == "result") {
                int end = at + t.Flat.Length, tag = (int)raw[at++];
                ReleaseFlat(t.Children[tag], raw, ref at); at = end; return;
            }
            if (t.Kind == "string" || t.Kind == "list") {
                int pointer = unchecked((int)raw[at++]), length = (int)raw[at++];
                if (t.Kind == "list") for (int i = 0; i < length; i++) {
                    var child = new List<ulong>(); LoadMemory(t.Children[0], Plus(pointer, checked(i * TypeAt(t.Children[0]).Size)), child);
                    int position = 0; ReleaseFlat(t.Children[0], child.ToArray(), ref position);
                }
                if (pointer != 0) Release(pointer); return;
            }
            at += t.Flat.Length;
        }
        public static object Parameter(int id, Type type, int input, int mode, ref int position)
        {
            var t = TypeAt(id); ulong[] raw;
            if (mode == 0) {
                raw = new ulong[t.Flat.Length];
                for (int i = 0; i < raw.Length; i++) raw[i] = *(ulong*)((byte*)(uint)input + 8 * position++);
            } else {
                position = Aligned(position, t.Align); var flat = new List<ulong>(); LoadMemory(id, Plus(input, position), flat);
                position = checked(position + t.Size); raw = flat.ToArray();
            }
            int at = 0; var value = Lift(id, type, raw, ref at); at = 0; ReleaseFlat(id, raw, ref at); return value;
        }
        public static void Result(int id, Type type, object value, int output)
        {
            var flat = new List<ulong>(); Lower(id, type, value, flat);
            if (TypeAt(id).Flat.Length <= 1) { if (flat.Count == 1) *(ulong*)(uint)output = flat[0]; }
            else {
                int ptr = Allocate(TypeAt(id).Size), at = 0; StoreMemory(id, ptr, flat.ToArray(), ref at); Put32(output, (uint)ptr);
            }
        }
        private static object LiftMemory(int id, Type type, int ptr)
        {
            var flat = new List<ulong>(); LoadMemory(id, ptr, flat); int at = 0; return Lift(id, type, flat.ToArray(), ref at);
        }
        private static object Lift(int id, Type type, ulong[] raw, ref int at)
        {
            var t = TypeAt(id);
            if (t.Kind == "type") return Lift(t.Children[0], type, raw, ref at);
            if (t.Kind == "unit") return default(Unit);
            if (t.Kind == "record" || t.Kind == "tuple") {
                var members = t.Names.Select(n => Member(type, n)).ToArray(); var values = new object[members.Length];
                for (int i = 0; i < members.Length; i++) values[i] = Lift(t.Children[i], MemberType(members[i]), raw, ref at);
                var constructor = type.GetConstructors().FirstOrDefault(c => c.GetParameters().Length == members.Length &&
                    c.GetParameters().All(p => { int i = Array.FindIndex(t.Names, n => NameKey(n) == NameKey(p.Name)); return i >= 0 && p.ParameterType == MemberType(members[i]); }));
                if (constructor != null) return constructor.Invoke(constructor.GetParameters().Select(p => values[Array.FindIndex(t.Names, n => NameKey(n) == NameKey(p.Name))]).ToArray());
                var record = Activator.CreateInstance(type);
                for (int i = 0; i < members.Length; i++) Set(members[i], record, values[i]);
                return record;
            }
            if (t.Kind == "option" || t.Kind == "result") {
                int end = at + t.Flat.Length; int tag = Length(raw[at++], 1); object value;
                if (t.Kind == "option") {
                    Type child = IsOption(type) || Nullable.GetUnderlyingType(type) != null ? type.GetGenericArguments()[0] : type;
                    object item = tag == 0 ? null : Lift(t.Children[1], child, raw, ref at);
                    if (!IsOption(type)) value = item;
                    else { value = Activator.CreateInstance(type); type.GetField("HasValue").SetValue(value, tag == 1); if (tag == 1) type.GetField("Value").SetValue(value, item); }
                } else {
                    value = Activator.CreateInstance(type); type.GetField("IsOk").SetValue(value, tag == 0);
                    type.GetField(tag == 0 ? "Value" : "Error").SetValue(value, Lift(t.Children[tag], type.GetGenericArguments()[tag], raw, ref at));
                }
                at = end; return value;
            }
            if (t.Kind == "flags") {
                ulong flags = 0; for (int i = 0; i < t.Flat.Length; i++) flags |= (raw[at++] & uint.MaxValue) << (i * 32);
                ulong bits = 0; for (int i = 0; i < t.Names.Length; i++) if ((flags & (1UL << i)) != 0) bits |= Bits(Enum.Parse(type, EnumName(type, t.Names[i])));
                return Enum.ToObject(type, bits);
            }
            ulong a = raw[at++];
            switch (t.Kind) {
                case "bool": return a != 0;
                case "u8": return (byte)a; case "s8": return unchecked((sbyte)a);
                case "u16": return (ushort)a; case "s16": return unchecked((short)a);
                case "u32": return (uint)a; case "s32": return unchecked((int)a);
                case "u64": return a; case "s64": return unchecked((long)a);
                case "f32": return BitConverter.Int32BitsToSingle(unchecked((int)a));
                case "f64": return BitConverter.Int64BitsToDouble(unchecked((long)a));
                case "char": return new Rune(checked((int)a));
                case "enum": return Enum.Parse(type, EnumName(type, t.Names[Length(a, t.Names.Length - 1)]));
                case "string": return Utf8.GetString((byte*)(uint)a, Length(raw[at++], MaxBytes));
                case "list":
                    int count = Length(raw[at++], MaxElements); var element = Element(type); var array = Array.CreateInstance(element, count);
                    int size = TypeAt(t.Children[0]).Size;
                    if ((long)size * count > MaxBytes) throw new ArgumentException("input list exceeds byte limit");
                    for (int i = 0; i < count; i++) array.SetValue(LiftMemory(t.Children[0], element, Plus((int)a, checked(i * size))), i);
                    return type.IsArray ? array : Activator.CreateInstance(type.IsInterface ? typeof(List<>).MakeGenericType(element) : type, new object[] { array });
                default: throw new ArgumentException("unsupported lift: " + t.Kind);
            }
        }
        private static void Lower(int id, Type type, object value, List<ulong> flat)
        {
            var t = TypeAt(id);
            if (t.Kind == "type") { Lower(t.Children[0], type, value, flat); return; }
            if (t.Kind == "unit") return;
            if (t.Kind == "record" || t.Kind == "tuple") {
                for (int i = 0; i < t.Children.Length; i++) { var m = Member(type, t.Names[i]); Lower(t.Children[i], MemberType(m), Get(m, value), flat); } return;
            }
            if (t.Kind == "option" || t.Kind == "result") {
                int end = flat.Count + t.Flat.Length;
                if (t.Kind == "option") {
                    bool wrapper = IsOption(type); bool some = wrapper ? (bool)type.GetField("HasValue").GetValue(value) : value != null;
                    flat.Add(some ? 1UL : 0UL);
                    if (some) Lower(t.Children[1], wrapper || Nullable.GetUnderlyingType(type) != null ? type.GetGenericArguments()[0] : type,
                        wrapper ? type.GetField("Value").GetValue(value) : value, flat);
                } else {
                    bool ok = (bool)type.GetField("IsOk").GetValue(value); flat.Add(ok ? 0UL : 1UL);
                    Lower(t.Children[ok ? 0 : 1], type.GetGenericArguments()[ok ? 0 : 1], type.GetField(ok ? "Value" : "Error").GetValue(value), flat);
                }
                while (flat.Count < end) flat.Add(0); return;
            }
            if (t.Kind == "flags") {
                ulong input = Bits(value), output = 0, mask = 0;
                for (int i = 0; i < t.Names.Length; i++) { ulong bit = Bits(Enum.Parse(type, EnumName(type, t.Names[i]))); mask |= bit; if ((input & bit) != 0) output |= 1UL << i; }
                if ((input & ~mask) != 0) throw new ArgumentException("C# flags contain values outside the WIT contract");
                for (int i = 0; i < t.Flat.Length; i++) flat.Add((uint)(output >> (i * 32))); return;
            }
            switch (t.Kind) {
                case "bool": flat.Add((bool)value ? 1UL : 0UL); break;
                case "u8": case "u16": case "u32": case "u64": flat.Add(Bits(value)); break;
                case "s8": case "s16": case "s32": flat.Add(unchecked((uint)Convert.ToInt32(value))); break;
                case "s64": flat.Add(unchecked((ulong)(long)value)); break;
                case "f32": flat.Add(unchecked((uint)BitConverter.SingleToInt32Bits((float)value))); break;
                case "f64": flat.Add(unchecked((ulong)BitConverter.DoubleToInt64Bits((double)value))); break;
                case "char": flat.Add((uint)((Rune)value).Value); break;
                case "enum":
                    int index = Array.FindIndex(t.Names, n => Enum.Parse(type, EnumName(type, n)).Equals(value));
                    if (index < 0) throw new ArgumentException("C# enum value is outside the WIT contract"); flat.Add((uint)index); break;
                case "string":
                    var bytes = Utf8.GetBytes((string)value ?? throw new ArgumentException("non-optional string is null"));
                    if (bytes.Length > MaxBytes) throw new ArgumentException("string exceeds size limit");
                    int ptr = Allocate(bytes.Length); bytes.CopyTo(new Span<byte>((void*)(uint)ptr, bytes.Length));
                    flat.Add((uint)ptr); flat.Add((uint)bytes.Length); break;
                case "list":
                    var list = new List<object>(); foreach (var item in (IEnumerable)value) { if (list.Count == MaxElements) throw new ArgumentException("list exceeds element limit"); list.Add(item); }
                    int size = TypeAt(t.Children[0]).Size, length = checked(size * list.Count);
                    if (length > MaxBytes) throw new ArgumentException("list exceeds byte limit");
                    ptr = Allocate(length); Type element = Element(type);
                    for (int i = 0; i < list.Count; i++) { var child = new List<ulong>(); Lower(t.Children[0], element, list[i], child); int at = 0; StoreMemory(t.Children[0], Plus(ptr, i * size), child.ToArray(), ref at); }
                    flat.Add((uint)ptr); flat.Add((uint)list.Count); break;
                default: throw new ArgumentException("unsupported lower: " + t.Kind);
            }
        }
        private static void LoadMemory(int id, int ptr, List<ulong> flat)
        {
            var t = TypeAt(id);
            if (t.Kind == "type") { LoadMemory(t.Children[0], ptr, flat); return; }
            if (t.Kind == "unit") return;
            if (t.Kind == "record" || t.Kind == "tuple") { for (int i = 0; i < t.Children.Length; i++) LoadMemory(t.Children[i], Plus(ptr, t.Offsets[i]), flat); return; }
            if (t.Kind == "option" || t.Kind == "result") {
                int end = flat.Count + t.Flat.Length; int tag = *(byte*)(uint)ptr;
                if (tag > 1) throw new ArgumentException("invalid variant tag"); flat.Add((uint)tag);
                LoadMemory(t.Children[tag], Plus(ptr, t.PayloadOffset), flat); while (flat.Count < end) flat.Add(0); return;
            }
            if (t.Kind == "list" || t.Kind == "string") { flat.Add(U32(ptr)); flat.Add(U32(Plus(ptr, 4))); return; }
            if (t.Kind == "flags" && t.Size > 4) { flat.Add(U32(ptr)); flat.Add(U32(Plus(ptr, 4))); return; }
            if (t.Flat.Length == 0) return;
            flat.Add(t.Size == 1 ? *(byte*)(uint)ptr : t.Size == 2 ? *(ushort*)(uint)ptr : t.Size == 4 ? U32(ptr) : *(ulong*)(uint)ptr);
        }
        private static void StoreMemory(int id, int ptr, ulong[] flat, ref int at)
        {
            var t = TypeAt(id);
            if (t.Kind == "type") { StoreMemory(t.Children[0], ptr, flat, ref at); return; }
            if (t.Kind == "unit") return;
            if (t.Kind == "record" || t.Kind == "tuple") { for (int i = 0; i < t.Children.Length; i++) StoreMemory(t.Children[i], Plus(ptr, t.Offsets[i]), flat, ref at); return; }
            if (t.Kind == "option" || t.Kind == "result") { int end = at + t.Flat.Length; int tag = checked((int)flat[at++]); *(byte*)(uint)ptr = (byte)tag; StoreMemory(t.Children[tag], Plus(ptr, t.PayloadOffset), flat, ref at); at = end; return; }
            if (t.Kind == "string" || t.Kind == "list") { Put32(ptr, (uint)flat[at++]); Put32(Plus(ptr, 4), (uint)flat[at++]); return; }
            if (t.Kind == "flags" && t.Size > 4) { Put32(ptr, (uint)flat[at++]); Put32(Plus(ptr, 4), (uint)flat[at++]); return; }
            if (t.Flat.Length == 0) return;
            ulong value = flat[at++];
            if (t.Size == 1) *(byte*)(uint)ptr = (byte)value; else if (t.Size == 2) *(ushort*)(uint)ptr = (ushort)value;
            else if (t.Size == 4) Put32(ptr, (uint)value); else *(ulong*)(uint)ptr = value;
        }
    }
}
