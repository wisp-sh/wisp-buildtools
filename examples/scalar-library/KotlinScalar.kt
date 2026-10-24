import org.teavm.interop.Export

@Export(name = "add")
fun add(a: Int, b: Int): Int = a + b

fun main() {}
