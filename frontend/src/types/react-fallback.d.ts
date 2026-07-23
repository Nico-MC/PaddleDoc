// Temporary fallback typings for environments without installed React packages.
// Remove this file once `react` and `@types/react` are installed.

declare module 'react' {
  type EffectCallback = () => void | (() => void);

  export function useEffect(effect: EffectCallback, deps?: readonly unknown[]): void;
  export function useState<T>(initialState: T | (() => T)): [T, (value: T | ((prev: T) => T)) => void];
}

declare module 'react/jsx-runtime' {
  export namespace JSX {
    interface IntrinsicElements {
      [elemName: string]: unknown;
    }
  }

  export const Fragment: unknown;
  export function jsx(type: unknown, props: unknown, key?: unknown): unknown;
  export function jsxs(type: unknown, props: unknown, key?: unknown): unknown;
}
