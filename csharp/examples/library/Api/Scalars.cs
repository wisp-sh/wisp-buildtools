namespace Example;
public static partial class Api
{
    public static byte EchoU8(byte value) => value;
    public static sbyte EchoS8(sbyte value) => value;
    public static ushort EchoU16(ushort value) => value;
    public static short EchoS16(short value) => value;
    public static uint EchoU32(uint value) => value;
    public static int EchoS32(int value) => value;
    public static ulong EchoU64(ulong value) => value;
    public static float EchoF32(float value) => value;
    public static double EchoF64(double value) => value;
    public static int Many(int a,int b,int c,int d,int e,int f,int g,int h,int i,int j,int k,int l,int m,int n,int o,int p,int q)
        => a+b+c+d+e+f+g+h+i+j+k+l+m+n+o+p+q;
}
