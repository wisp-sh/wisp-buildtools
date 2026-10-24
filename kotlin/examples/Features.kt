import org.teavm.interop.Export

data class Box<T>(val value: T)

interface Increment {
    fun apply(value: Int): Int
}

class AddOne : Increment {
    override fun apply(value: Int): Int = when (val boxed: Any = value) {
        is Int if boxed >= 0 -> boxed + 1
        else -> error("expected a nonnegative integer")
    }
}

@Export(name = "answer")
fun featureAnswer(): Int {
    val operation: Increment = AddOne()
    val transform: (Int) -> Int = { operation.apply(it) }
    val boxed = Box(listOf(19, 21))
    return boxed.value.map(transform).sum()
}

fun main() {
    println("kotlin-features-startup")
}
