using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using Wisp;

internal static class WasmModule
{
    private sealed class Reader
    {
        public readonly byte[] Bytes; public int At;
        public Reader(byte[] bytes) { Bytes = bytes; }
        public byte Byte() { if (At >= Bytes.Length) throw new ArgumentException("truncated Wasm"); return Bytes[At++]; }
        public uint U32() { uint value = 0; for (int shift = 0; shift <= 28; shift += 7) { byte b = Byte(); if (shift == 28 && b > 15) throw new ArgumentException("invalid Wasm integer"); value |= (uint)(b & 127) << shift; if ((b & 128) == 0) return value; } throw new ArgumentException("invalid Wasm integer"); }
        public int S32() { int value = 0, shift = 0; byte b; do { b = Byte(); value |= (b & 127) << shift; shift += 7; if (shift > 35) throw new ArgumentException("invalid Wasm offset"); } while ((b & 128) != 0); if (shift < 32 && (b & 64) != 0) value |= -1 << shift; return value; }
        public byte[] Take(int count) { if (count < 0 || Bytes.Length - At < count) throw new ArgumentException("truncated Wasm payload"); var result = Bytes.AsSpan(At, count).ToArray(); At += count; return result; }
        public string Name() => Encoding.UTF8.GetString(Take(checked((int)U32())));
        public byte[] Rest() => Take(Bytes.Length - At);
    }
    private static void U32(Stream stream, uint value) { do { byte b = (byte)(value & 127); value >>= 7; stream.WriteByte((byte)(b | (value == 0 ? 0 : 128))); } while (value != 0); }
    private static void S32(Stream stream, int value) { bool more; do { byte b = (byte)(value & 127); value >>= 7; more = !((value == 0 && (b & 64) == 0) || (value == -1 && (b & 64) != 0)); stream.WriteByte((byte)(b | (more ? 128 : 0))); } while (more); }
    private static void Name(Stream stream, string value) { byte[] bytes = Encoding.UTF8.GetBytes(value); U32(stream, (uint)bytes.Length); stream.Write(bytes); }
    private static void Section(Stream stream, byte id, byte[] payload) { stream.WriteByte(id); U32(stream, (uint)payload.Length); stream.Write(payload); }
    private static byte[] Vector(uint count, byte[] original, IEnumerable<byte[]> appended) { using var output = new MemoryStream(); U32(output, count); output.Write(original); foreach (var bytes in appended) output.Write(bytes); return output.ToArray(); }
    private static byte[] FunctionType(byte[] parameters, byte[] results) { using var output = new MemoryStream(); output.WriteByte(0x60); U32(output, (uint)parameters.Length); output.Write(parameters); U32(output, (uint)results.Length); output.Write(results); return output.ToArray(); }
    private static byte[] Export(string name, uint function) { using var output = new MemoryStream(); Name(output, name); output.WriteByte(0); U32(output, function); return output.ToArray(); }
    private static byte[] Body(Action<MemoryStream> write) { using var output = new MemoryStream(); write(output); using var body = new MemoryStream(); U32(body, (uint)output.Length); body.Write(output.ToArray()); return body.ToArray(); }
    private static void Call(Stream output, uint index) { output.WriteByte(0x10); U32(output, index); }
    private static void Local(Stream output, int index) { output.WriteByte(0x20); U32(output, (uint)index); }
    private static void Constant(Stream output, int value) { output.WriteByte(0x41); S32(output, value); }
    private static void SetLocal(Stream output, int index) { output.WriteByte(0x21); U32(output, (uint)index); }
    private static void Memory(Stream output, byte operation, byte lane, int offset) { output.WriteByte(operation); U32(output, lane == Canonical.I64 || lane == Canonical.F64 ? 3U : 2U); U32(output, (uint)offset); }
    private static byte Store(byte lane) => lane == Canonical.I32 ? (byte)0x36 : lane == Canonical.I64 ? (byte)0x37 : lane == Canonical.F32 ? (byte)0x38 : (byte)0x39;
    private static byte Load(byte lane) => lane == Canonical.I32 ? (byte)0x28 : lane == Canonical.I64 ? (byte)0x29 : lane == Canonical.F32 ? (byte)0x2a : (byte)0x2b;
    private static void Limits(Reader r) { uint flags = r.U32(); if ((flags & ~3U) != 0) throw new ArgumentException("unsupported runtime memory limits"); r.U32(); if ((flags & 1) != 0) r.U32(); }

    public static byte[] Package(byte[] template, byte[] bundle, Bindings bindings)
    {
        if (bundle.Length > 16 * 1024 * 1024) throw new ArgumentException("managed bundle exceeds 16 MiB");
        var reader = new Reader(template); if (!reader.Take(8).SequenceEqual(new byte[] { 0, 97, 115, 109, 1, 0, 0, 0 })) throw new ArgumentException("invalid runtime template");
        var sections = new List<(byte Id, byte[] Payload)>();
        while (reader.At < template.Length) { byte id = reader.Byte(); sections.Add((id, reader.Take(checked((int)reader.U32())))); }
        byte[] Get(byte id) => sections.Single(s => s.Id == id).Payload;
        var typeReader = new Reader(Get(1)); uint typeCount = typeReader.U32(); byte[] originalTypes = typeReader.Rest();
        typeReader = new Reader(originalTypes);
        for (int i = 0; i < typeCount; i++) { if (typeReader.Byte() != 0x60) throw new ArgumentException("unsupported runtime type group"); typeReader.Take(checked((int)typeReader.U32())); typeReader.Take(checked((int)typeReader.U32())); }
        var imports = new Reader(Get(2)); uint importCount = imports.U32(), importedFunctions = 0;
        for (int i = 0; i < importCount; i++) {
            imports.Name(); imports.Name(); byte kind = imports.Byte();
            switch (kind) { case 0: imports.U32(); importedFunctions++; break; case 1: imports.Byte(); Limits(imports); break; case 2: Limits(imports); break; case 3: imports.Byte(); imports.Byte(); break; case 4: imports.Byte(); imports.U32(); break; default: throw new ArgumentException("unsupported runtime import"); }
        }
        var functions = new Reader(Get(3)); uint functionCount = functions.U32(); byte[] originalFunctions = functions.Rest();
        var exportsReader = new Reader(Get(7)); uint exportCount = exportsReader.U32(); byte[] originalExports = exportsReader.Bytes.AsSpan(exportsReader.At).ToArray();
        var exports = new Dictionary<string, uint>();
        for (int i = 0; i < exportCount; i++) { string name = exportsReader.Name(); byte kind = exportsReader.Byte(); uint index = exportsReader.U32(); if (kind == 0) exports.Add(name, index); }
        uint begin = exports["__wisp_csharp_begin"], invoke = exports["__wisp_csharp_invoke"], end = exports["__wisp_csharp_end"], release = exports["__wisp_csharp_release"];
        var typesAdded = new List<byte[]>(); var functionsAdded = new List<byte[]>(); var exportsAdded = new List<byte[]>(); var bodiesAdded = new List<byte[]>();
        uint AddFunction(string name, byte[] parameters, byte[] results, byte[] body) {
            if (exports.ContainsKey(name)) throw new ArgumentException("export name conflicts with runtime: " + name);
            uint index = importedFunctions + functionCount + (uint)functionsAdded.Count;
            using var type = new MemoryStream(); U32(type, typeCount + (uint)typesAdded.Count);
            functionsAdded.Add(type.ToArray()); typesAdded.Add(FunctionType(parameters, results)); exportsAdded.Add(Export(name, index)); bodiesAdded.Add(body); exports.Add(name, index); return index;
        }
        for (int i = 0; i < bindings.Exports.Count; i++) {
            var binding = bindings.Exports[i]; int fn = i;
            var flat = binding.Parameters.SelectMany(p => bindings.Types[p].Flat).ToArray(); bool indirect = flat.Length > 16;
            byte[] parameters = indirect ? new[] { Canonical.I32 } : flat;
            byte[] result = binding.Result < 0 ? Array.Empty<byte>() : bindings.Types[binding.Result].Flat;
            bool indirectResult = result.Length > 1;
            byte[] returns = indirectResult ? new[] { Canonical.I32 } : result;
            int argsLocal = parameters.Length, outputLocal = argsLocal + 1;
            var body = Body(output => {
                U32(output, 1); U32(output, 2); output.WriteByte(Canonical.I32);
                Call(output, begin); SetLocal(output, argsLocal);
                if (!indirect) for (int j = 0; j < flat.Length; j++) { Local(output, argsLocal); Local(output, j); Memory(output, Store(flat[j]), flat[j], 8 * j); }
                Constant(output, fn); Local(output, indirect ? 0 : argsLocal); Constant(output, indirect ? 1 : 0); Call(output, invoke); SetLocal(output, outputLocal);
                if (indirect) { Local(output, 0); Call(output, release); }
                if (returns.Length != 0) { Local(output, outputLocal); Memory(output, Load(returns[0]), returns[0], 0); }
                if (!indirectResult) Call(output, end);
                output.WriteByte(0x0b);
            });
            AddFunction(binding.Wire, parameters, returns, body);
            if (indirectResult) AddFunction("cabi_post_" + binding.Wire, new[] { Canonical.I32 }, Array.Empty<byte>(), Body(output => { U32(output, 0); Call(output, end); output.WriteByte(0x0b); }));
        }
        var code = new Reader(Get(10)); uint codeCount = code.U32();
        if (codeCount != functionCount) throw new ArgumentException("runtime code/function count mismatch");
        var replacement = new Dictionary<byte, byte[]> {
            [1] = Vector(typeCount + (uint)typesAdded.Count, originalTypes, typesAdded),
            [3] = Vector(functionCount + (uint)functionsAdded.Count, originalFunctions, functionsAdded),
            [7] = Vector(exportCount + (uint)exportsAdded.Count, originalExports, exportsAdded),
            [10] = Vector(codeCount + (uint)bodiesAdded.Count, code.Rest(), bodiesAdded)
        };
        var data = new Reader(Get(11)); uint dataCount = data.U32(); var segments = new List<byte[]>(); byte[] suffix = null; int replaced = 0;
        byte[] marker = Encoding.ASCII.GetBytes("WISP_CSHARP_BUNDLE_V2");
        byte[] Segment(int offset, byte[] bytes) { using var output = new MemoryStream(); U32(output, 0); Constant(output, offset); output.WriteByte(0x0b); U32(output, (uint)bytes.Length); output.Write(bytes); return output.ToArray(); }
        for (int i = 0; i < dataCount; i++) {
            int start = data.At; uint flags = data.U32(); int offset = 0;
            if (flags != 1) { if (flags == 2) { if (data.U32() != 0) throw new ArgumentException("unsupported data memory"); } else if (flags != 0) throw new ArgumentException("unsupported data flags"); if (data.Byte() != 0x41) throw new ArgumentException("non-constant runtime data offset"); offset = data.S32(); if (data.Byte() != 0x0b) throw new ArgumentException("invalid data offset expression"); }
            byte[] contents = data.Take(checked((int)data.U32())); int position = contents.AsSpan().IndexOf(marker);
            if (position < 0) { segments.Add(data.Bytes.AsSpan(start, data.At - start).ToArray()); continue; }
            if (flags == 1 || ++replaced != 1 || contents.Length - position < 36 + 16 * 1024 * 1024 || contents.AsSpan(position + marker.Length).IndexOf(marker) >= 0) throw new ArgumentException("invalid runtime payload slot");
            byte[] prefix = contents.AsSpan(0, position + 36).ToArray();
            System.Buffers.Binary.BinaryPrimitives.WriteInt32LittleEndian(prefix.AsSpan(position + 32, 4), bundle.Length);
            segments.Add(Segment(offset, prefix.Concat(bundle).ToArray()));
            int tail = position + 36 + 16 * 1024 * 1024;
            if (tail < contents.Length) suffix = Segment(checked(offset + tail), contents.AsSpan(tail).ToArray());
        }
        if (replaced != 1) throw new ArgumentException("runtime payload slot is missing");
        if (suffix != null) segments.Add(suffix);
        replacement[11] = Vector((uint)segments.Count, Array.Empty<byte>(), segments);
        if (sections.Any(s => s.Id == 12)) { using var count = new MemoryStream(); U32(count, (uint)segments.Count); replacement[12] = count.ToArray(); }
        using var module = new MemoryStream(); module.Write(template.AsSpan(0, 8));
        foreach (var section in sections) Section(module, section.Id, replacement.TryGetValue(section.Id, out var payload) ? payload : section.Payload);
        return module.ToArray();
    }
}
