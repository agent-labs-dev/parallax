/* px-clock: run a command, and everything it starts, on the world's wall clock.
 *
 *   px-clock OFFSET_FILE COMMAND [ARG]...
 *
 * OFFSET_FILE holds one decimal number: seconds to add to the real wall clock (negative for a
 * world in the past). It is re-read while the tree runs, so the runner can move the clock
 * forward at a scheduler fire without restarting the harness.
 *
 * Why not LD_PRELOAD (libfaketime): some harness binaries are static musl executables,
 * which no preload reaches. Instead every process in the tree gets a seccomp filter that traps
 * the wall-clock syscalls (clock_gettime on the REALTIME clocks, gettimeofday, time) to this
 * tracer, which shifts the result on the way out. libc normally answers those calls from the
 * vDSO without a syscall, so at each exec the vDSO's auxv entry is hidden, making libc (glibc,
 * musl, Go's runtime) fall back to the real syscall. Monotonic clocks are never touched, so
 * timeouts and durations stay real.
 *
 * Linux x86_64 and aarch64 only.
 */
#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#if defined(__x86_64__)
#define AUDIT_ARCH_SELF AUDIT_ARCH_X86_64
#define REG_NR(r) ((r).orig_rax)
#define REG_ARG0(r) ((r).rdi)
#define REG_ARG1(r) ((r).rsi)
#define REG_RET(r) ((r).rax)
#define REG_SP(r) ((r).rsp)
#elif defined(__aarch64__)
#define AUDIT_ARCH_SELF AUDIT_ARCH_AARCH64
#define REG_NR(r) ((r).regs[8])
#define REG_ARG0(r) ((r).regs[0])
#define REG_ARG1(r) ((r).regs[1])
#define REG_RET(r) ((r).regs[0])
#define REG_SP(r) ((r).sp)
#else
#error "px-clock supports x86_64 and aarch64"
#endif

#define NSEC 1000000000LL
#define RELOAD_NS 200000000LL /* how stale the offset may be after the runner rewrites it */
#define SLOTS 16384

/* Per-thread state: startup SIGSTOP not yet swallowed; the syscall entry awaiting its exit. */
struct slot {
    pid_t tid;
    int started;
    long nr;
    unsigned long arg0, arg1;
};

static struct slot slots[SLOTS];
static const char *offset_path;
static long long offset_ns;
static struct timespec offset_mtime;
static ino_t offset_ino;
static long long checked_at;

static struct slot *slot_for(pid_t tid) {
    unsigned i = (unsigned)tid % SLOTS;
    for (unsigned n = 0; n < SLOTS; n++, i = (i + 1) % SLOTS) {
        if (slots[i].tid == tid) return &slots[i];
        if (slots[i].tid == 0) {
            memset(&slots[i], 0, sizeof slots[i]);
            slots[i].tid = tid;
            slots[i].nr = -1;
            return &slots[i];
        }
    }
    fprintf(stderr, "px-clock: more than %d live threads\n", SLOTS);
    exit(125);
}

static void forget(pid_t tid) {
    /* Backward-shift deletion keeps linear probing correct without tombstones. */
    unsigned i = (unsigned)tid % SLOTS;
    for (unsigned n = 0; n < SLOTS && slots[i].tid != tid; n++, i = (i + 1) % SLOTS)
        if (slots[i].tid == 0) return;
    if (slots[i].tid != tid) return;
    slots[i].tid = 0;
    for (unsigned j = (i + 1) % SLOTS; slots[j].tid != 0; j = (j + 1) % SLOTS) {
        unsigned home = (unsigned)slots[j].tid % SLOTS;
        int between = (i <= j) ? (i < home && home <= j) : (i < home || home <= j);
        if (!between) {
            slots[i] = slots[j];
            slots[j].tid = 0;
            i = j;
        }
    }
}

static long long mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * NSEC + ts.tv_nsec;
}

/* Returns 0 on success; the previous offset is kept when a rewrite is caught half-written. */
static int load_offset(void) {
    struct stat st;
    if (stat(offset_path, &st) != 0) return -1;
    /* A rewrite is a rename onto the path, so the inode changes even when two land in one mtime tick. */
    if (st.st_ino == offset_ino && st.st_mtim.tv_sec == offset_mtime.tv_sec &&
        st.st_mtim.tv_nsec == offset_mtime.tv_nsec)
        return 0;
    FILE *f = fopen(offset_path, "r");
    if (!f) return -1;
    char buf[64] = {0};
    size_t len = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[len] = 0;
    char *end;
    errno = 0;
    double seconds = strtod(buf, &end);
    if (end == buf || errno != 0) return -1;
    offset_ns = (long long)(seconds * 1e9);
    offset_mtime = st.st_mtim;
    offset_ino = st.st_ino;
    return 0;
}

static long long current_offset(void) {
    long long now = mono_ns();
    if (now - checked_at >= RELOAD_NS) {
        load_offset();
        checked_at = now;
    }
    return offset_ns;
}

static int get_regs(pid_t tid, struct user_regs_struct *regs) {
    struct iovec iov = {regs, sizeof *regs};
    return (int)ptrace(PTRACE_GETREGSET, tid, (void *)NT_PRSTATUS, &iov);
}

#ifdef SYS_time /* its only caller; aarch64 has no time(2), and -Werror rejects an unused function */
static int set_regs(pid_t tid, struct user_regs_struct *regs) {
    struct iovec iov = {regs, sizeof *regs};
    return (int)ptrace(PTRACE_SETREGSET, tid, (void *)NT_PRSTATUS, &iov);
}
#endif

static int peek(pid_t tid, unsigned long addr, void *out, size_t len) {
    struct iovec local = {out, len}, remote = {(void *)addr, len};
    return process_vm_readv(tid, &local, 1, &remote, 1, 0) == (ssize_t)len ? 0 : -1;
}

static int poke(pid_t tid, unsigned long addr, const void *in, size_t len) {
    struct iovec local = {(void *)in, len}, remote = {(void *)addr, len};
    return process_vm_writev(tid, &local, 1, &remote, 1, 0) == (ssize_t)len ? 0 : -1;
}

static void shift(long long *sec, long long *frac, long long unit) {
    long long off = current_offset();
    long long total = *frac * (NSEC / unit) + off % NSEC;
    *sec += off / NSEC + total / NSEC;
    total %= NSEC;
    if (total < 0) {
        total += NSEC;
        *sec -= 1;
    }
    *frac = total / (NSEC / unit);
}

static void on_syscall_exit(pid_t tid, struct slot *s) {
    struct user_regs_struct regs;
    if (s->nr < 0 || get_regs(tid, &regs) != 0) return;
    long ret = (long)REG_RET(regs);
    if (s->nr == SYS_clock_gettime || s->nr == SYS_gettimeofday) {
        unsigned long addr = s->nr == SYS_clock_gettime ? s->arg1 : s->arg0;
        if (ret != 0 || addr == 0) goto done;
        long long pair[2];
        if (peek(tid, addr, pair, sizeof pair) != 0) goto done;
        shift(&pair[0], &pair[1], s->nr == SYS_clock_gettime ? NSEC : 1000000);
        poke(tid, addr, pair, sizeof pair);
    }
#ifdef SYS_time
    else if (s->nr == SYS_time && ret >= 0) {
        long long sec = ret, frac = 0;
        shift(&sec, &frac, NSEC);
        REG_RET(regs) = (unsigned long long)sec;
        set_regs(tid, &regs);
        if (s->arg0 != 0) poke(tid, s->arg0, &sec, sizeof sec);
    }
#endif
done:
    s->nr = -1;
}

/* After exec: turn AT_SYSINFO_EHDR into AT_IGNORE so libc cannot find the vDSO. */
static void hide_vdso(pid_t tid) {
    struct user_regs_struct regs;
    if (get_regs(tid, &regs) != 0) return;
    unsigned long p = REG_SP(regs), word;
    if (peek(tid, p, &word, sizeof word) != 0) return;
    p += (word + 2) * sizeof word; /* argc, argv[argc], NULL */
    do {                           /* envp until NULL */
        if (peek(tid, p, &word, sizeof word) != 0) return;
        p += sizeof word;
    } while (word != 0);
    for (int i = 0; i < 64; i++, p += 2 * sizeof word) {
        if (peek(tid, p, &word, sizeof word) != 0 || word == AT_NULL) return;
        if (word == AT_SYSINFO_EHDR) {
            unsigned long ignore = AT_IGNORE;
            poke(tid, p, &ignore, sizeof ignore);
            return;
        }
    }
}

static void install_filter(void) {
    struct sock_filter prog[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_SELF, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_gettimeofday, 8, 0),
#ifdef SYS_time
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_time, 7, 0),
#else
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_gettimeofday, 7, 0),
#endif
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clock_gettime, 0, 5),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, CLOCK_REALTIME, 4, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, CLOCK_REALTIME_COARSE, 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, CLOCK_REALTIME_ALARM, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, CLOCK_TAI, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRACE),
    };
    struct sock_fprog fprog = {sizeof prog / sizeof prog[0], prog};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 || prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &fprog) != 0) {
        perror("px-clock: seccomp");
        _exit(125);
    }
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: px-clock OFFSET_FILE COMMAND [ARG]...\n");
        return 125;
    }
    offset_path = argv[1];
    if (load_offset() != 0) {
        fprintf(stderr, "px-clock: cannot read a clock offset from %s\n", offset_path);
        return 125;
    }
    checked_at = mono_ns();
    pid_t child = fork();
    if (child < 0) {
        perror("px-clock: fork");
        return 125;
    }
    if (child == 0) {
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) != 0) {
            perror("px-clock: ptrace");
            _exit(125);
        }
        raise(SIGSTOP);
        install_filter();
        execvp(argv[2], argv + 2);
        fprintf(stderr, "px-clock: %s: %s\n", argv[2], strerror(errno));
        _exit(errno == ENOENT ? 127 : 126);
    }
    /* The tracer must outlive the command: a tracee whose tracer is gone gets ENOSYS from every
     * wall-clock read. Box processes run under setsid with px-clock as the group leader, and
     * parallax signals the whole group, so the command still receives these itself. */
    signal(SIGINT, SIG_IGN);
    signal(SIGTERM, SIG_IGN);
    signal(SIGHUP, SIG_IGN);
    signal(SIGQUIT, SIG_IGN);
    int status;
    if (waitpid(child, &status, 0) != child || !WIFSTOPPED(status)) return 125;
    slot_for(child)->started = 1;
    long opts = PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACESECCOMP | PTRACE_O_TRACECLONE | PTRACE_O_TRACEFORK |
                PTRACE_O_TRACEVFORK | PTRACE_O_TRACEEXEC;
    if (ptrace(PTRACE_SETOPTIONS, child, 0, (void *)opts) != 0) {
        perror("px-clock: PTRACE_SETOPTIONS");
        kill(child, SIGKILL);
        return 125;
    }
    ptrace(PTRACE_CONT, child, 0, 0);
    for (;;) {
        pid_t tid = waitpid(-1, &status, __WALL);
        if (tid < 0) {
            if (errno == EINTR) continue;
            return 125;
        }
        if (WIFEXITED(status) || WIFSIGNALED(status)) {
            forget(tid);
            /* Tracees left running are detached by the kernel when this process exits. */
            if (tid == child) return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
            continue;
        }
        if (!WIFSTOPPED(status)) continue;
        struct slot *s = slot_for(tid);
        int sig = WSTOPSIG(status), event = status >> 16, inject = 0;
        if (sig == (SIGTRAP | 0x80)) {
            on_syscall_exit(tid, s);
        } else if (sig == SIGTRAP && event == PTRACE_EVENT_SECCOMP) {
            struct user_regs_struct regs;
            if (get_regs(tid, &regs) == 0) {
                s->nr = (long)REG_NR(regs);
                s->arg0 = REG_ARG0(regs);
                s->arg1 = REG_ARG1(regs);
                ptrace(PTRACE_SYSCALL, tid, 0, 0); /* stop again at this syscall's exit */
                continue;
            }
        } else if (sig == SIGTRAP && event == PTRACE_EVENT_EXEC) {
            hide_vdso(tid);
        } else if (sig == SIGTRAP && event != 0) {
            unsigned long fresh;
            if (ptrace(PTRACE_GETEVENTMSG, tid, 0, &fresh) == 0) (void)slot_for((pid_t)fresh);
        } else if (sig == SIGSTOP && !s->started) {
            s->started = 1; /* an auto-attached thread's first stop, not a signal to deliver */
        } else {
            inject = sig;
        }
        s->started = 1;
        ptrace(PTRACE_CONT, tid, 0, (void *)(long)inject);
    }
}
