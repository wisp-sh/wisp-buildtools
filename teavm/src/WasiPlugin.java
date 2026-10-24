package wisp.java;

import java.util.Set;
import java.util.TreeSet;
import org.teavm.backend.c.TeaVMCHost;
import org.teavm.backend.wasm.dependencies.WasmGCExportDependencyListener;
import org.teavm.dependency.DependencyAgent;
import org.teavm.interop.Export;
import org.teavm.vm.spi.TeaVMHost;
import org.teavm.vm.spi.TeaVMPlugin;

/** Preserve scalar exports when using the C backend for WASI. */
public final class WasiPlugin implements TeaVMPlugin {
    static final Set<String> exports = new TreeSet<>();

    @Override
    public void install(TeaVMHost host) {
        if (host.getExtension(TeaVMCHost.class) == null) return;
        host.add(new WasmGCExportDependencyListener() {
            @Override
            public void classReached(DependencyAgent agent, String name) {
                super.classReached(agent, name);
                for (var method : agent.getClassSource().get(name).getMethods()) {
                    var annotation = method.getAnnotations().get(Export.class.getName());
                    if (annotation == null) continue;
                    String exported = annotation.getValue("name").getString();
                    if (!exported.matches("[A-Za-z_][A-Za-z0-9_]*")) {
                        throw new IllegalArgumentException("C backend export must be an identifier: " + exported);
                    }
                    exports.add(exported);
                }
            }
        });
    }
}
