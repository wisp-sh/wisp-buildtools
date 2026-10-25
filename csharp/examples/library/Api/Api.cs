using System.Text;
using Wisp;
namespace Example;
public static partial class Api
{
#if LIBRARY_EXAMPLE && NET6_0_OR_GREATER && WISP && WASI
    public static long Add(long left, long right) => Helpers.Sum(left, right);
#endif
    public static string Greet(string name) => $"Hello, {name}!";
    public static Result<Person, string> Advance(Person value) => value.Age == 0
        ? Result<Person, string>.Err("age must be positive")
        : Result<Person, string>.Ok(value with { Age = value.Age + 1 });
    public static byte[] Choose(Option<Mode> value) => new byte[] {
        value.HasValue ? (byte)(value.Value == Mode.High ? 42 : 7) : (byte)0 };
    public static Access Permissions(Access value) => value | Access.Write;
    public static Rune EchoChar(Rune value) => value;
    public static (string, int) Pair(string text, int value) => (text, value);
    public static List<Person> People(List<Person> values) => values.Select(p => p with { Age = p.Age + 1 }).ToList();
    public static string? Maybe(string? value) => value;
    public static double? Scale(double? value) => value * 2;
    public static Result<float, long> Number(Result<float, long> value) => value;
    public static bool Flip(bool value) => !value;
    public static void Nothing() { }
}
