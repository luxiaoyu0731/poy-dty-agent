/** Bound how long a caller waits; the underlying request is not cancelled. */
export function requestDeadline<T>(request: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(() => {
      reject(new Error(`${label} 暂未在 ${Math.round(ms / 1000)} 秒内返回`));
    }, ms);
    request.then(
      (value) => {
        globalThis.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        globalThis.clearTimeout(timer);
        reject(error);
      }
    );
  });
}
