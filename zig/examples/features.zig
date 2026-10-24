const std = @import("std");
const Pair = struct { left: i32, right: i32 };
fn sum(comptime T: type, values: []const T) T {
    var total: T = 0;
    for (values) |value| total += value;
    return total;
}
export fn answer() i32 {
    const p: Pair = .{ .left = 19, .right = 23 };
    const values = [_]i32{ p.left, p.right, 9 };
    return std.math.clamp(sum(i32, &values), 0, 42);
}
export fn twice(value: f64) f64 { return value * 2; }
