const arithmetic = @import("modules/arithmetic.zig");
export fn answer() i32 {
    const values = [_]i32{ 12, 13, 17 };
    return arithmetic.sum(i32, &values);
}
