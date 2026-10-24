import org.teavm.interop.Export;

public final class JavaScalar {
    @Export(name = "add")
    public static int add(int a, int b) {
        return a + b;
    }

    public static void main(String[] arguments) {}
}
