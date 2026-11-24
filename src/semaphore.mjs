/** Minimal counting semaphore. The lane allows one metered turn per account. */
export function makeSemaphore(permits = 1) {
  let available = Math.max(1, permits);
  const queue = [];

  const release = () => {
    const next = queue.shift();
    if (next) next();
    else available++;
  };

  return {
    get available() {
      return available;
    },
    get waiting() {
      return queue.length;
    },
    acquire(signal) {
      if (signal?.aborted) return Promise.reject(signal.reason);
      if (available > 0) {
        available--;
        return Promise.resolve(release);
      }
      return new Promise((resolve, reject) => {
        const entry = () => {
          signal?.removeEventListener("abort", onAbort);
          resolve(release);
        };
        const onAbort = () => {
          const index = queue.indexOf(entry);
          if (index !== -1) queue.splice(index, 1);
          reject(signal.reason);
        };
        queue.push(entry);
        signal?.addEventListener("abort", onAbort, { once: true });
      });
    },
  };
}