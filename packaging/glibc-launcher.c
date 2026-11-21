/* glibc-launcher: starts a program of the LinDVB AppImage with the AppImage's
 * own C library, so that the AppImage also runs on systems with an older
 * glibc than the one it was built on (or none at all, e.g. musl).
 *
 * build-appdir.sh moves each program to .real/<name> and puts this launcher
 * in its place. The launcher runs the dynamic loader explicitly:
 *
 *   <loader> --library-path <libs> --argv0 <argv[0]> .real/<name> args...
 *
 * where <loader> is usr/lib/glibc/ld-linux-x86-64.so.2 of the AppDir and
 * <libs> is usr/lib/glibc:usr/lib (then the host's LD_LIBRARY_PATH). When
 * AppRun found a glibc on the host that is at least as new as the bundled one
 * (LINDVB_HOST_GLIBC=1), it uses the host's loader and C library instead, so
 * that graphics drivers built for a newer glibc keep working.
 *
 * The library path is given to the loader, not exported: programs of the host
 * that LinDVB starts (xdg-open, bwrap, ...) keep using the host's libraries.
 * Statically linked, so it needs neither a loader nor a shell, and also works
 * inside WebKit's bubblewrap sandbox. */
#define _GNU_SOURCE
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define HOST_LOADER "/lib64/ld-linux-x86-64.so.2"

int main(int argc, char **argv)
{
    char exe[PATH_MAX], root[PATH_MAX], real[PATH_MAX], loader[PATH_MAX];
    char *libpath;

    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n <= 0) { perror("glibc-launcher: /proc/self/exe"); return 127; }
    exe[n] = '\0';
    char *slash = strrchr(exe, '/');
    if (!slash) return 127;
    *slash = '\0';
    if (snprintf(real, sizeof real, "%s/.real/%s", exe, slash + 1) >= (int)sizeof real) return 127;

    /* the AppDir: the nearest parent directory with usr/lib/glibc */
    strcpy(root, exe);
    for (;;) {
        if (snprintf(loader, sizeof loader, "%s/usr/lib/glibc/ld-linux-x86-64.so.2", root) < (int)sizeof loader
            && access(loader, X_OK) == 0)
            break;
        char *s = strrchr(root, '/');
        if (!s || s == root) { fprintf(stderr, "glibc-launcher: no usr/lib/glibc above %s\n", exe); return 127; }
        *s = '\0';
    }

    const char *host = getenv("LD_LIBRARY_PATH");
    const char *sep = host && *host ? ":" : "";
    if (!host) host = "";
    const char *use_host = getenv("LINDVB_HOST_GLIBC");
    int r;
    if (use_host && strcmp(use_host, "1") == 0 && access(HOST_LOADER, X_OK) == 0) {
        strcpy(loader, HOST_LOADER);
        r = asprintf(&libpath, "%s/usr/lib%s%s", root, sep, host);
    } else {
        r = asprintf(&libpath, "%s/usr/lib/glibc:%s/usr/lib%s%s", root, root, sep, host);
    }
    if (r < 0) return 127;

    char **args = calloc((size_t)argc + 6, sizeof *args);
    if (!args) return 127;
    int i = 0;
    args[i++] = loader;
    args[i++] = "--library-path";
    args[i++] = libpath;
    args[i++] = "--argv0";
    args[i++] = argv[0];
    args[i++] = real;
    for (int a = 1; a < argc; a++) args[i++] = argv[a];
    args[i] = NULL;
    execv(loader, args);
    fprintf(stderr, "glibc-launcher: cannot run %s: ", loader);
    perror(NULL);
    return 127;
}
