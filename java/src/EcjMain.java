package wisp.java;

import java.io.File;
import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import org.eclipse.jdt.internal.compiler.CompilationResult;
import org.eclipse.jdt.internal.compiler.DefaultErrorHandlingPolicies;
import org.eclipse.jdt.internal.compiler.ICompilerRequestor;
import org.eclipse.jdt.internal.compiler.batch.CompilationUnit;
import org.eclipse.jdt.internal.compiler.batch.FileSystem;
import org.eclipse.jdt.internal.compiler.env.ICompilationUnit;
import org.eclipse.jdt.internal.compiler.impl.CompilerOptions;
import org.eclipse.jdt.internal.compiler.problem.DefaultProblemFactory;
import org.eclipse.jdt.internal.compiler.util.HashtableOfInt;

/** Run ECJ against the explicit portable API SDK, without a host JDK module image. */
public final class EcjMain {
    private static HashtableOfInt templates;

    /** Use the pinned English messages directly, without resource bundle discovery. */
    private static final class Problems extends DefaultProblemFactory {
        Problems() { super(Locale.ROOT); }
        @Override public void setLocale(Locale locale) { messageTemplates = templates; }
        @Override public Locale getLocale() { return Locale.ROOT; }
    }

    private static void trace(String message) {
        if (Boolean.getBoolean("wisp.compiler.trace")) {
            System.err.println("ECJ: " + message);
        }
    }

    public static void main(String[] args) throws Exception {
        try {
            compile(args);
        } catch (Throwable error) {
            error.printStackTrace();
            throw new IllegalStateException("Java compiler failed", error);
        }
    }

    private static void compile(String[] args) throws Exception {
        trace("enter");
        if (args.length < 5) {
            throw new IllegalArgumentException("usage: EcjMain OUTPUT CLASSPATH SOURCE_LEVEL MESSAGES SOURCE...");
        }
        Properties messages = new Properties();
        try (var input = new FileInputStream(args[3])) { messages.load(input); }
        templates = new HashtableOfInt(messages.size());
        for (String name : messages.stringPropertyNames()) {
            try { templates.put(Integer.parseInt(name) + 1, messages.getProperty(name)); }
            catch (NumberFormatException ignored) { /* upstream metadata has nonnumeric entries */ }
        }
        String[] names = new String[args.length - 4];
        ICompilationUnit[] units = new ICompilationUnit[names.length];
        for (int index = 0; index < names.length; index++) {
            names[index] = args[index + 4];
            char[] contents;
            try (FileInputStream input = new FileInputStream(names[index])) {
                ByteArrayOutputStream collected = new ByteArrayOutputStream();
                byte[] buffer = new byte[8192];
                int count;
                while ((count = input.read(buffer)) >= 0) {
                    collected.write(buffer, 0, count);
                }
                contents = new String(collected.toByteArray(), StandardCharsets.UTF_8).toCharArray();
            }
            units[index] = new CompilationUnit(contents, names[index], "UTF-8");
        }
        trace("source inputs read");
        FileSystem environment = new FileSystem(args[1].split(File.pathSeparator), names, "UTF-8");
        trace("classpath initialized");
        Map<String, String> settings = new HashMap<>();
        settings.put(CompilerOptions.OPTION_Compliance, args[2]);
        settings.put(CompilerOptions.OPTION_Source, args[2]);
        settings.put(CompilerOptions.OPTION_TargetPlatform, args[2]);
        settings.put(CompilerOptions.OPTION_Process_Annotations, CompilerOptions.DISABLED);
        settings.put(CompilerOptions.OPTION_Store_Annotations, CompilerOptions.ENABLED);
        final boolean[] failed = {false};
        final File output = new File(args[0]);
        ICompilerRequestor requestor = new ICompilerRequestor() {
            @Override public void acceptResult(CompilationResult result) {
                if (result.hasErrors()) {
                    failed[0] = true;
                }
                if (result.getProblems() != null) {
                    for (var problem : result.getProblems()) {
                        System.err.println(new String(problem.getOriginatingFileName()) + ":"
                                + problem.getSourceLineNumber() + ": " + problem.getMessage());
                    }
                }
                if (!result.hasErrors()) {
                    for (var file : result.getClassFiles()) {
                        File path = new File(output, new String(file.fileName()) + ".class");
                        try {
                            if (!path.getParentFile().isDirectory() && !path.getParentFile().mkdirs()) {
                                throw new IOException("could not create " + path.getParent());
                            }
                            try (FileOutputStream stream = new FileOutputStream(path)) {
                                stream.write(file.getBytes());
                            }
                        } catch (IOException error) {
                            throw new IllegalStateException(error);
                        }
                    }
                }
            }
        };
        org.eclipse.jdt.internal.compiler.Compiler compiler = new org.eclipse.jdt.internal.compiler.Compiler(
                environment, DefaultErrorHandlingPolicies.proceedWithAllProblems(), new CompilerOptions(settings),
                requestor, new Problems());
        trace("compiler initialized");
        compiler.useSingleThread = true;
        try {
            compiler.compile(units);
        } finally {
            environment.cleanup();
        }
        if (failed[0]) {
            throw new IllegalStateException("Java source compilation failed");
        }
    }
}
