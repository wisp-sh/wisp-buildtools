import org.teavm.interop.Export

@Export(name = "add")
fun add(a: Int, b: Int): Int = a + b

@Export(name = "widen")
fun widen(value: Long): Long = value + 1L

@Export(name = "scale")
fun scale(value: Double): Double = value * 1.5

@Export(name = "overflow")
fun overflow(value: Int): Int = if (value + 1 > value) 1 else 0

fun main() {
    println("kotlin-startup")
}
