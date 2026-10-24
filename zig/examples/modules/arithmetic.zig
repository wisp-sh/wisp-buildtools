pub fn sum(comptime T: type, values: []const T) T {
    var result: T = 0;
    for (values) |value| result += value;
    return result;
}
