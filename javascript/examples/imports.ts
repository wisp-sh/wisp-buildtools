import hostMath from 'toolchain:javascript/host-math';

export function add(a: number, b: number): number {
  return hostMath.add(a, b);
}
