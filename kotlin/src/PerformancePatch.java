package wisp.bootstrap;

import java.nio.file.Files;
import java.nio.file.Path;
import org.jetbrains.org.objectweb.asm.ClassReader;
import org.jetbrains.org.objectweb.asm.ClassWriter;
import org.jetbrains.org.objectweb.asm.Opcodes;
import org.jetbrains.org.objectweb.asm.tree.*;

/** Avoid initializing unavailable JVM management APIs when detailed profiling is off. */
public final class PerformancePatch implements Opcodes {
    public static void main(String[] args) throws Exception {
        var type = new ClassNode();
        new ClassReader(Files.readAllBytes(Path.of(args[0]))).accept(type, 0);
        if (!type.name.equals("org/jetbrains/kotlin/util/PerformanceManager")) {
            throw new IllegalStateException("unexpected Kotlin performance class");
        }
        int patched = 0;
        for (var method : type.methods) {
            if (!method.name.equals("initializeCurrentThread") || !method.desc.equals("()V")) continue;
            MethodInsnNode factory = null;
            AbstractInsnNode end = null;
            int factories = 0, enables = 0, returns = 0;
            for (var instruction : method.instructions) {
                if (instruction instanceof MethodInsnNode call) {
                    if (call.owner.equals("java/lang/management/ManagementFactory")
                            && call.name.equals("getThreadMXBean")) { factory = call; factories++; }
                    if (call.owner.equals("java/lang/management/ThreadMXBean")
                            && call.name.equals("setThreadCpuTimeEnabled")) enables++;
                }
                if (instruction.getOpcode() == RETURN) { end = instruction; returns++; }
            }
            if (factories != 1 || enables != 1 || returns != 1
                    || !(factory.getPrevious() instanceof VarInsnNode receiver)
                    || receiver.getOpcode() != ALOAD || receiver.var != 0) {
                throw new IllegalStateException("Kotlin profiler setup differs from pinned bytecode");
            }
            var done = new LabelNode();
            var guard = new InsnList();
            guard.add(new VarInsnNode(ALOAD, 0));
            guard.add(new FieldInsnNode(GETFIELD, type.name, "detailedPerf", "Z"));
            guard.add(new JumpInsnNode(IFEQ, done));
            method.instructions.insertBefore(factory.getPrevious(), guard);
            method.instructions.insertBefore(end, done);
            patched++;
        }
        if (patched != 1) throw new IllegalStateException("expected one Kotlin profiling setup method");
        var writer = new ClassWriter(ClassWriter.COMPUTE_FRAMES | ClassWriter.COMPUTE_MAXS);
        type.accept(writer);
        Files.write(Path.of(args[1]), writer.toByteArray());
    }
}
