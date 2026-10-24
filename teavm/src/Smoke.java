import org.teavm.interop.Export;

public final class Smoke {
    @Export(name = "answer")
    public static int answer() { return 42; }

    public static void main(String[] arguments) {
        System.out.println("teavm-startup");
    }
}
