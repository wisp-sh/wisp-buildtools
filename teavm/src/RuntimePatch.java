package wisp.bootstrap;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.TreeMap;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.InsnNode;
import org.objectweb.asm.tree.IntInsnNode;
import org.objectweb.asm.tree.JumpInsnNode;
import org.objectweb.asm.tree.LdcInsnNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.MethodNode;

/** Repair two pinned TeaVM 0.16.0 allocator boundary checks at bootstrap. */
public final class RuntimePatch implements Opcodes {
    private static final String GC = "org/teavm/runtime/GC.class";

    private static AbstractInsnNode[] expected(MethodNode method, int... opcodes) {
        var instructions = new ArrayList<AbstractInsnNode>();
        for (var instruction : method.instructions) {
            if (instruction.getOpcode() >= 0) instructions.add(instruction);
        }
        int[] actual = instructions.stream().mapToInt(AbstractInsnNode::getOpcode).toArray();
        if (!Arrays.equals(actual, opcodes)) {
            throw new IllegalStateException("TeaVM GC method differs from pinned bytecode: " + method.name);
        }
        return instructions.toArray(AbstractInsnNode[]::new);
    }

    private static byte[] patch(byte[] original) {
        var type = new ClassNode(ASM9);
        new ClassReader(original).accept(type, 0);
        int count = 0;
        for (var method : type.methods) {
            if (method.name.equals("hasAvailableMemory") && method.desc.equals("(I)Z")) {
                var code = expected(method, GETSTATIC, GETFIELD, ILOAD, IF_ICMPEQ,
                        GETSTATIC, GETFIELD, ILOAD, BIPUSH, IADD, IF_ICMPGT,
                        ILOAD, INVOKESTATIC, IFEQ, ICONST_1, GOTO, ICONST_0, IRETURN);
                if (((IntInsnNode) code[7]).operand != 8
                        || !((MethodInsnNode) code[11]).name.equals("getNextChunkIfPossible")) {
                    throw new IllegalStateException("TeaVM minimum chunk size differs from pinned runtime");
                }
                // A remainder of exactly one free-chunk header is valid.
                ((JumpInsnNode) code[9]).setOpcode(IF_ICMPGE);
                count++;
            } else if (method.name.equals("computeMinRequestedSize") && method.desc.equals("(I)J")) {
                var code = expected(method, GETSTATIC, GETFIELD, IFNE, ILOAD, GETSTATIC,
                        GETFIELD, ISUB, ISTORE, INVOKESTATIC, ILOAD, I2L, LADD, LRETURN);
                if (!((MethodInsnNode) code[8]).name.equals("availableBytes")) {
                    throw new IllegalStateException("TeaVM growth calculation differs from pinned runtime");
                }
                // Heap growth rounds to eight bytes. Include a complete free
                // header so a four-byte allocation remainder cannot cause OOM.
                method.instructions.insertBefore(code[12], new LdcInsnNode(8L));
                method.instructions.insertBefore(code[12], new InsnNode(LADD));
                count++;
            }
        }
        if (count != 2) throw new IllegalStateException("expected exactly two TeaVM GC repairs");
        var writer = new ClassWriter(ClassWriter.COMPUTE_MAXS);
        type.accept(writer);
        return writer.toByteArray();
    }

    public static void main(String[] args) throws Exception {
        var files = new TreeMap<String, byte[]>();
        try (var input = new ZipFile(args[0])) {
            var entries = input.entries();
            while (entries.hasMoreElements()) {
                var entry = entries.nextElement();
                try (var stream = input.getInputStream(entry)) {
                    files.put(entry.getName(), stream.readAllBytes());
                }
            }
        }
        if (!files.containsKey(GC)) throw new IllegalStateException("missing TeaVM GC class");
        files.put(GC, patch(files.get(GC)));
        try (var output = new ZipOutputStream(Files.newOutputStream(Path.of(args[1])))) {
            output.setLevel(9);
            for (var entry : files.entrySet()) {
                var item = new ZipEntry(entry.getKey());
                item.setTimeLocal(LocalDateTime.of(1980, 1, 1, 0, 0));
                output.putNextEntry(item);
                output.write(entry.getValue());
                output.closeEntry();
            }
        }
    }
}
