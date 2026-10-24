export const data = {
  summarize(values: number[], label: string) {
    return {
      count: values.length,
      total: values.reduce((total, value) => total + value, 0),
      label: `Summary: ${label}`,
    };
  },
  safeDivide(a: number, b: number): number {
    if (b === 0) throw 'division by zero';
    return a / b;
  },
};
