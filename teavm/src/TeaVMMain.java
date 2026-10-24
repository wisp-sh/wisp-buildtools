package wisp.java;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.nio.file.Files;
import java.nio.file.Path;
import org.teavm.tooling.TeaVMTool;
import org.teavm.tooling.TeaVMTargetType;
import org.teavm.vm.TeaVMOptimizationLevel;
import org.teavm.diagnostics.DefaultProblemTextConsumer;

/** Small batch entry point. The same bytecode runs on the bootstrap and Wasm JVM. */
public final class TeaVMMain {
    public static void main(String[] args) throws Exception {
        if (args.length < 4) {
            throw new IllegalArgumentException("usage: TeaVMMain MAIN OUTPUT_DIRECTORY OUTPUT_NAME CLASS_PATH...");
        }
        List<File> classPath = new ArrayList<>();
        for (int index = 3; index < args.length; index++) {
            classPath.add(new File(args[index]));
        }
        TeaVMTool compiler = new TeaVMTool();
        compiler.setTargetType(TeaVMTargetType.C);
        compiler.setEntryPointName("wisp_java_main");
        compiler.setMainClass(args[0]);
        compiler.setTargetDirectory(new File(args[1]));
        compiler.setTargetFileName(args[2]);
        compiler.setClassPath(classPath);
        compiler.setOptimizationLevel(TeaVMOptimizationLevel.SIMPLE);
        compiler.setObfuscated(false);
        compiler.generate();
        if (!compiler.getProblemProvider().getSevereProblems().isEmpty()) {
            for (var problem : compiler.getProblemProvider().getSevereProblems()) {
                var message = new DefaultProblemTextConsumer();
                problem.render(message);
                System.err.println(message.getText());
            }
            throw new IllegalStateException("TeaVM could not compile the application");
        }
        Files.write(Path.of(args[1], "exports.txt"), WasiPlugin.exports);
    }
}
