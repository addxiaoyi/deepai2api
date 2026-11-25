// 跨进程安全的小工具。
//
// 这个仓库里，同一份 state 可能被三个进程同时碰：网关、MCP server、CLI。
// 文件写入本身是原子的（tmp + rename），但"读-改-写"不是，所以每个写入点都要
// 先拿一把基于 `O_EXCL` 的锁，再决定写什么。锁只保护毫秒级的写窗口，不做业务互斥
// （业务互斥见 allmodels-register 的 job 锁）。
import { closeSync, openSync, readFileSync, statSync, unlinkSync, writeSync } from "node:fs";

const sleepSync = (ms) => {
  const shared = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(shared, 0, 0, ms);
};

export const lockPathFor = (target) => `${target}.lock`;

export function isProcessAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

/**
 * Run `fn` while holding `<target>.lock`.
 *
 * @returns {{ ok: true, value: any } | { ok: false, reason: "busy" | "error", error?: Error }}
 */
export function withFileLock(target, fn, { timeoutMs = 5_000, staleMs = 30_000 } = {}) {
  const lockPath = lockPathFor(target);
  const deadline = Date.now() + timeoutMs;

  for (;;) {
    try {
      const fd = openSync(lockPath, "wx");
      try {
        writeSync(fd, JSON.stringify({ pid: process.pid, at: Date.now() }));
      } finally {
        closeSync(fd);
      }
      break;
    } catch (error) {
      if (error.code !== "EEXIST") return { ok: false, reason: "error", error };
      // Held. Reclaim only when the owner is gone or the lock is older than staleMs.
      try {
        const info = JSON.parse(readFileSync(lockPath, "utf8"));
        const age = Date.now() - Number(info.at ?? 0);
        if (age > staleMs && !isProcessAlive(Number(info.pid))) {
          unlinkSync(lockPath);
          continue;
        }
      } catch {
        // Unreadable lock file: it can only be a leftover of a crash.
        try {
          unlinkSync(lockPath);
        } catch {
          // someone else reclaimed it first
        }
        continue;
      }
      if (Date.now() >= deadline) return { ok: false, reason: "busy" };
      sleepSync(20);
    }
  }

  try {
    return { ok: true, value: fn() };
  } catch (error) {
    return { ok: false, reason: "error", error };
  } finally {
    try {
      unlinkSync(lockPath);
    } catch {
      // already gone
    }
  }
}

/** True when `path` was written after `sinceMs` (with a small mtime tolerance). */
export function isNewerThan(path, sinceMs, toleranceMs = 2) {
  try {
    return statSync(path).mtimeMs > sinceMs + toleranceMs;
  } catch {
    return false;
  }
}
