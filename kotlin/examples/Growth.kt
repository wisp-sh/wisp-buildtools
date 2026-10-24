import org.teavm.interop.Export

private lateinit var retained: ByteArray

@Export(name = "answer")
fun answer(): Int {
    var sum = 0
    var index = 0
    while (index < retained.size) {
        sum += retained[index].toInt() and 255
        index += 4096
    }
    return if (sum == 86016) 42 else -1
}

fun main() {
    retained = ByteArray(8 * 1024 * 1024)
    var index = 0
    while (index < retained.size) {
        retained[index] = 42
        index += 4096
    }
    println("kotlin-growth-startup")
}
