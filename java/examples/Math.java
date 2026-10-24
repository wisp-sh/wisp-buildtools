import org.teavm.interop.Export;

public final class Math {
    @Export(name = "add")
    public static int add(int a, int b) {
        return a + b;
    }

    @Export(name = "widen")
    public static long widen(long value) {
        return value + 1L;
    }

    @Export(name = "scale")
    public static double scale(double value) {
        return value * 1.5;
    }

    @Export(name = "overflow")
    public static int overflow(int value) {
        return value + 1 > value ? 1 : 0;
    }

    public static void main(String[] arguments) {
        System.out.println("java-startup");
    }
}
