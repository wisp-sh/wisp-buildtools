import java.lang.management.ManagementFactory;
import java.io.RandomAccessFile;
import java.io.FileInputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;

public final class JvmSmoke {
    static class Factory {
        Object create() { return null; }
    }
    static final class RealFactory extends Factory {
        Object create() { return new ArrayList<String>(); }
    }
    static final class Initialization {
        static String events = "";
        static int mark(String value) { events += value; return 1; }
    }
    interface PlainParent { int INITIALIZED = Initialization.mark("plain;"); }
    interface PlainChild extends PlainParent { int INITIALIZED = Initialization.mark("child;"); }
    static final class UsesPlain implements PlainParent {}
    interface AbstractValue { String value(); }
    interface DefaultRoot extends AbstractValue {
        int INITIALIZED = Initialization.mark("root;");
        default String value() { return "root"; }
    }
    interface DefaultChild extends DefaultRoot {
        int INITIALIZED = Initialization.mark("default-child;");
        default String value() { return "most-specific"; }
    }
    static final class UsesDefault implements DefaultChild {}
    static class StaticOwner {
        static int VALUE = Initialization.mark("owner;");
        static int inheritedMethod() { return 7; }
    }
    static final class StaticAlias extends StaticOwner {
        static int VALUE_IN_ALIAS = Initialization.mark("alias;");
    }
    interface FieldOwner { int VALUE = Initialization.mark("field-owner;"); }
    static final class FieldAlias implements FieldOwner {
        static int INITIALIZED = Initialization.mark("field-alias;");
    }
    interface VirtualContract {
        int INITIALIZED = Initialization.mark("virtual-contract;");
        String call();
    }
    static final class VirtualImplementation implements VirtualContract {
        public String call() { return "virtual"; }
    }

    public static void main(String[] arguments) throws Exception {
        Factory factory = new RealFactory();
        if (!(factory.create() instanceof ArrayList)) throw new AssertionError("virtual reference return");
        Initialization.events = "";
        new UsesPlain();
        if (!Initialization.events.isEmpty()) throw new AssertionError("class initialized non-default interface");
        int initialized = PlainChild.INITIALIZED;
        if (!Initialization.events.equals("child;")) throw new AssertionError("interface initialized parent");
        AbstractValue defaultValue = new UsesDefault();
        if (!Initialization.events.equals("child;root;default-child;")) throw new AssertionError("default-interface initialization order");
        if (!defaultValue.value().equals("most-specific")) throw new AssertionError("most-specific interface default");
        Initialization.events = "";
        if (StaticAlias.VALUE != 1 || StaticAlias.inheritedMethod() != 7)
            throw new AssertionError("inherited static members");
        if (!Initialization.events.equals("owner;")) throw new AssertionError("static member initialized alias");
        Initialization.events = "";
        if (FieldAlias.VALUE != 1 || !Initialization.events.equals("field-owner;"))
            throw new AssertionError("interface field initialized alias");
        Initialization.events = "";
        VirtualContract contract = new VirtualImplementation();
        if (!contract.call().equals("virtual") || !Initialization.events.isEmpty())
            throw new AssertionError("virtual resolution initialized interface");
        if (!String.class.getModule().getName().equals("java.base")) throw new AssertionError("base module");
        if (!ManagementFactory.class.getModule().getName().equals("java.management")) throw new AssertionError("management module");
        if (JvmSmoke.class.getModule().isNamed()) throw new AssertionError("application unnamed module");
        if (JvmSmoke.class.getClassLoader() == null) throw new AssertionError("application loader");
        if (RealFactory.class.getNestHost() != JvmSmoke.class) throw new AssertionError("nest host loader");
        try {
            throw new IllegalArgumentException("caught in Wasm");
        } catch (IllegalArgumentException expected) {
            if (!expected.getMessage().equals("caught in Wasm")) throw new AssertionError("exception payload");
        }
        Path path = Path.of(arguments[0]);
        if (Files.exists(path.resolveSibling("missing"))) throw new AssertionError("missing file exists");
        Files.writeString(path, "jvm-wasm-io\n");
        if (!Files.readString(path).equals("jvm-wasm-io\n")) throw new AssertionError("file I/O");
        try (RandomAccessFile file = new RandomAccessFile(path.toFile(), "r")) {
            if (file.read() != 'j') throw new AssertionError("random file read");
            if (file.length() != 12 || file.getFilePointer() != 1) throw new AssertionError("length preserves position");
            file.seek(4);
            if (file.read() != 'w' || file.getFilePointer() != 5) throw new AssertionError("random file seek");
            if (file.getChannel().size() != 12 || file.getFilePointer() != 5) throw new AssertionError("channel size preserves position");
        }
        Path binary = path.resolveSibling("bytes.bin");
        Files.write(binary, new byte[] {0, 127, (byte) 128, (byte) 255});
        try (FileInputStream input = new FileInputStream(binary.toFile())) {
            for (int expected : new int[] {0, 127, 128, 255, -1})
                if (input.read() != expected) throw new AssertionError("unsigned single-byte input");
        }
        if (!System.getProperty("java.home").equals("/runtime/jdk")) throw new AssertionError("provider home");
        System.out.println("jvm-wasm-ok");
    }
}
