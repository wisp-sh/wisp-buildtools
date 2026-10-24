import org.teavm.interop.Export;

public final class Growth {
    private static byte[] retained;

    @Export(name = "answer")
    public static int answer() {
        int sum = 0;
        for (int index = 0; index < retained.length; index += 4096) {
            sum += retained[index] & 255;
        }
        return sum == 86016 ? 42 : -1;
    }

    public static void main(String[] arguments) {
        retained = new byte[8 * 1024 * 1024];
        for (int index = 0; index < retained.length; index += 4096) {
            retained[index] = 42;
        }
        if (answer() != 42) {
            throw new AssertionError("array contents changed during heap growth");
        }
        System.out.println("java-growth-startup");
    }
}
