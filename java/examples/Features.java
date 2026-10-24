import java.util.List;
import org.teavm.interop.Export;

public final class Features {
    private record Pair(int first, int second) {}

    @Export(name = "answer")
    public static int answer() {
        var pair = new Pair(20, 22);
        List<Integer> values = List.of(pair.first(), pair.second());
        try {
            values.get(2);
            return -1;
        } catch (IndexOutOfBoundsException expected) {
            return values.stream().mapToInt(value -> value.intValue()).sum();
        }
    }

    public static void main(String[] arguments) {
        System.out.println("java-record-startup");
    }
}
