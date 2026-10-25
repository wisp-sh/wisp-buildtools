namespace Example;
public record Person(string Name, uint Age);
public enum Mode { Low = 7, High = 103 }
[System.Flags] public enum Access : ulong { Read = 1UL << 40, Write = 1UL << 60 }
public static class Helpers { public static long Sum(long left, long right) => left + right; }
